"""
Limitations:
1. Cannot whitelist IPs for this service (for only client to be able to access)
2. All ports open by default (instead, could map the port dynamically)? [may be fixed on the service, not the pod]
"""

import asyncio
import base64
import json
import logging
import os
from typing import Optional

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger: logging.Logger = logging.getLogger("kubebox")


class KubeboxPodExistsError(Exception):
    """Exception raised when a pod already exists."""


class KubeboxPod:
    def __init__(self, name: str, namespace: str, kubebox: "Kubebox" = None):
        self.name = name
        self.namespace = namespace
        self._kubebox = kubebox

    async def wait_until_ready(self, poll_interval: float = 0.1):
        start_time = asyncio.get_event_loop().time()
        while True:
            try:
                pod = await asyncio.to_thread(
                    self._kubebox._core_v1.read_namespaced_pod,
                    name=self.name,
                    namespace=self.namespace,
                )
                status = pod.status
                if status.phase == "Running":
                    # Check if containers are ready
                    all_ready = all(c.ready for c in status.container_statuses)
                    if all_ready:
                        elapsed = asyncio.get_event_loop().time() - start_time
                        logger.info(
                            f"Pod '{self.name}' is ready. Time taken: {elapsed:.2f} seconds."
                        )
                        return elapsed
            except ApiException as e:
                logger.error(f"Exception when reading pod status: {e}")
                break
            await asyncio.sleep(poll_interval)

    async def destroy(self):
        await asyncio.to_thread(
            self._kubebox._core_v1.delete_namespaced_pod,
            name=self.name,
            namespace=self.namespace,
        )

    def __str__(self):
        return f"KubeboxPod(name={self.name}, namespace={self.namespace})"

    def __repr__(self):
        return str(self)


class KubeboxService:
    def __init__(self, name: str, namespace: str, kubebox: "Kubebox" = None):
        self.name = name
        self.namespace = namespace
        self._kubebox = kubebox

    async def update_network_policy(self, allowed_ips: list[str]):
        policy_name = f"{self.name}-network-policy"
        network_policy_manifest = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": policy_name},
            "spec": {
                "podSelector": {"matchLabels": {"app": self.name}},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [
                    {
                        "from": [
                            {"ipBlock": {"cidr": f"{allowed_ip}/32"}}
                            for allowed_ip in allowed_ips
                        ]
                    }
                ],
                "egress": [
                    {
                        "to": [
                            {"ipBlock": {"cidr": f"{allowed_ip}/32"}}
                            for allowed_ip in allowed_ips
                        ]
                    }
                ],
            },
        }

        try:
            await asyncio.to_thread(
                self._kubebox._networking_v1.replace_namespaced_network_policy,
                name=policy_name,
                namespace=self.namespace,
                body=network_policy_manifest,
            )
            logger.info(f"Network Policy '{policy_name}' updated.")
        except ApiException as e:
            if e.status == 404:
                try:
                    await asyncio.to_thread(
                        self._kubebox._networking_v1.create_namespaced_network_policy,
                        namespace=self.namespace,
                        body=network_policy_manifest,
                    )
                    logger.info(f"Network Policy '{policy_name}' created.")
                except ApiException as create_e:
                    logger.error(f"Exception when creating network policy: {create_e}")
            else:
                logger.error(f"Exception when updating network policy: {e}")

    async def wait_until_ready(self, poll_interval: float = 0.1):
        logger.info(f"Waiting for service '{self.name}' to be ready...")
        # start_time = asyncio.get_event_loop().time()
        while True:  # asyncio.get_event_loop().time() - start_time < timeout:
            try:
                # Use asyncio.to_thread to run the blocking I/O operation in a separate thread
                service = await asyncio.to_thread(
                    self._kubebox._core_v1.read_namespaced_service,
                    name=self.name,
                    namespace=self.namespace,
                )
                ingress = service.status.load_balancer.ingress
                if ingress:
                    ip = ingress[0].ip or ingress[0].hostname
                    if ip:
                        logger.info(f"Service '{self.name}' is ready with IP: {ip}.")
                        return ip
            except ApiException as e:
                logger.error(f"Exception when reading service status: {e}")
                # Consider whether to break or continue based on the type of exception

            await asyncio.sleep(poll_interval)

        raise TimeoutError(f"Timed out waiting for service '{self.name}' to be ready.")

    async def get_external_ip(self, timeout: float = 5, poll_interval: float = 0.1):
        logger.info(f"Waiting for external IP of service '{self.name}'...")
        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            try:
                # Use asyncio.to_thread to run the blocking I/O operation in a separate thread
                service = await asyncio.to_thread(
                    self._kubebox._core_v1.read_namespaced_service,
                    name=self.name,
                    namespace=self.namespace,
                )
                ingress = service.status.load_balancer.ingress
                if ingress:
                    ip = ingress[0].ip or ingress[0].hostname
                    if ip:
                        logger.info(f"Service '{self.name}' is available at {ip}.")
                        return ip
            except ApiException as e:
                logger.error(f"Exception when reading service status: {e}")
                # Consider whether to break or continue based on the type of exception

            await asyncio.sleep(poll_interval)

        raise TimeoutError(
            f"Timed out waiting for external IP of service '{self.name}'."
        )

    async def destroy(self):
        await asyncio.to_thread(
            self._kubebox._core_v1.delete_namespaced_service,
            name=self.name,
            namespace=self.namespace,
        )

    def __str__(self):
        return f"KubeboxService(name={self.name}, namespace={self.namespace})"

    def __repr__(self):
        return str(self)


class Kubebox:
    def __init__(
        self,
        kubebox_str: Optional[str] = None,
        terraform_path: Optional[str] = None,
        print_kubebox_str: bool = False,
    ):
        self.terraform_path = terraform_path
        self._load_kube_config_from_terraform(
            terraform_path, kubebox_str, print_kubebox_str
        )

        self._client = client.ApiClient()
        self._core_v1 = client.CoreV1Api()

    def create_secret(self, secret_name: str, namespace: str, data: dict[str, str]):
        # Encode the secret data in base64
        encoded_data = {
            k: base64.b64encode(v.encode()).decode() for k, v in data.items()
        }

        secret_manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, "namespace": namespace},
            "type": "Opaque",
            "data": encoded_data,
        }

        try:
            self._core_v1.create_namespaced_secret(
                namespace=namespace, body=secret_manifest
            )
            logger.info(
                f"Secret {secret_name} created successfully in namespace {namespace}"
            )
        except ApiException as e:
            if e.status == 409:
                logger.info(
                    f"Secret {secret_name} already exists in namespace {namespace}"
                )
                try:
                    self._core_v1.replace_namespaced_secret(
                        namespace=namespace, name=secret_name, body=secret_manifest
                    )
                    logger.info(
                        f"Secret {secret_name} updated successfully in namespace {namespace}"
                    )
                except ApiException as update_e:
                    logger.error(f"Error updating secret {secret_name}: {update_e}")
                    raise update_e
            else:
                logger.error(f"Error creating secret {secret_name}: {e}")
                raise e

    async def get_all_pods(self, namespace: str = "default") -> list[KubeboxPod]:
        pods = await asyncio.to_thread(
            self._core_v1.list_namespaced_pod, namespace=namespace
        )
        return [
            KubeboxPod(pod.metadata.name, pod.metadata.namespace, kubebox=self)
            for pod in pods.items
        ]

    async def get_all_services(
        self, namespace: str = "default"
    ) -> list[KubeboxService]:
        services = await asyncio.to_thread(
            self._core_v1.list_namespaced_service, namespace=namespace
        )
        return [
            KubeboxService(
                service.metadata.name, service.metadata.namespace, kubebox=self
            )
            for service in services.items
        ]

    def create_pod(
        self,
        pod_name: str,
        namespace: str = "default",
        image: str = "speckai/sandbox:latest",
        username: str = None,
        ports: list[int] = None,
        kubebox_public_key_secret_name: str = None,
        kubebox_public_key_key: str = None,
    ):
        if ports is None:
            ports = []
        # The sandbox needs to listen on port 80 for the API. Currently unused as we expose all ports on the Docker image.
        if 80 in ports:
            raise ValueError(
                "Port 80 is reserved for the API and cannot be used by the sandbox."
            )
        ports = [80] + ports

        logger.info(
            f"Creating pod: {pod_name} in namespace: {namespace} with image: {image}"
        )

        labels = {"app": pod_name}
        if username:
            labels["username"] = username

        pod_manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": namespace, "labels": labels},
            "spec": {
                "containers": [
                    {
                        "name": pod_name,
                        "image": image,
                        "ports": [{"containerPort": port} for port in ports],
                    }
                ],
            },
        }

        if kubebox_public_key_secret_name and kubebox_public_key_key:
            pod_manifest["spec"]["containers"][0]["env"] = [
                {
                    "name": kubebox_public_key_key,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": kubebox_public_key_secret_name,
                            "key": kubebox_public_key_key,
                        }
                    },
                }
            ]

        try:
            self._core_v1.create_namespaced_pod(namespace=namespace, body=pod_manifest)
            logger.info(f"Pod {pod_name} created successfully")
            return KubeboxPod(pod_name, namespace, kubebox=self)
        except ApiException as e:
            if e.status == 409:
                logger.info(f"Pod {pod_name} already exists in namespace {namespace}")
                logger.warning(
                    f"Updating pod metadata currently not supported, please delete and recreate pod with `kubectl delete pod {pod_name} -n {namespace}`"
                )
                return KubeboxPod(pod_name, namespace, kubebox=self)
            else:
                logger.error(f"Error creating pod {pod_name}: {e}")
                raise e

    def create_service(
        self,
        pod_name: str,
        namespace: str = "default",
        username: str = None,
        ports: list[int] = None,
    ):
        if ports is None:
            ports = []
        logger.info(f"Creating service: {pod_name}-service in namespace: {namespace}")

        labels = {"app": pod_name}
        if username:
            labels["username"] = username

        service_name = f"{pod_name}-service"
        service_manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": service_name,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "selector": {"app": pod_name},
                "ports": [
                    {"name": "api", "protocol": "TCP", "port": 80, "targetPort": 80},
                    *[
                        {
                            "name": f"dev-{port}",
                            "protocol": "TCP",
                            "port": port,
                            "targetPort": port,
                        }
                        for port in ports
                    ],
                ],
                "type": "LoadBalancer",
                "externalTrafficPolicy": "Local",
            },
        }

        try:
            self._core_v1.create_namespaced_service(
                namespace=namespace, body=service_manifest
            )
            logger.info(f"Service {service_name} created successfully")
            return KubeboxService(service_name, namespace, kubebox=self)
        except ApiException as e:
            if e.status == 409:
                logger.info(
                    f"Service {service_name} already exists in namespace {namespace}"
                )
                try:
                    # Patch the existing service (instead of replacing it)
                    self._core_v1.patch_namespaced_service(
                        name=service_name, namespace=namespace, body=service_manifest
                    )
                    logger.info(f"Service {service_name} updated successfully")
                    return KubeboxService(service_name, namespace, kubebox=self)
                except ApiException as update_e:
                    logger.error(f"Error updating service {service_name}: {update_e}")
                    raise update_e
            else:
                logger.error(f"Error creating service {service_name}: {e}")
                raise e

    def _load_kube_config_from_terraform(
        self, tfstate_file, kubebox_str, print_dict: bool = False
    ):
        if tfstate_file:
            with open(tfstate_file, "r") as f:
                tfstate = json.load(f)

            # Navigate the state file to find the kubeconfig data
            resources = tfstate.get("resources", [])
            kube_config = None

            for resource in resources:
                if resource.get("type") == "azurerm_kubernetes_cluster":
                    for instance in resource.get("instances", []):
                        attributes = instance.get("attributes", {})
                        # Check for 'kube_config_raw' or 'kube_admin_config_raw'
                        kube_config_raw = attributes.get(
                            "kube_config_raw"
                        ) or attributes.get("kube_admin_config_raw")
                        if kube_config_raw:
                            # Decode the base64-encoded kubeconfig
                            # kube_config = base64.b64decode(kube_config_raw).decode('utf-8')
                            kube_config = kube_config_raw
                            break
                    if kube_config:
                        break

            if not kube_config:
                raise ValueError(
                    "Failed to find kube_config in terraform state file. Please ensure your terraform state file contains a valid Azure Kubernetes Service resource with kube_config_raw or kube_admin_config_raw."
                )

            # Write the kubeconfig to a temporary file
            kubeconfig_path = "/tmp/kubeconfig"
            os.makedirs(os.path.dirname(kubeconfig_path), exist_ok=True)
            with open(kubeconfig_path, "w") as f:
                f.write(kube_config)

            if print_dict:
                logger.debug(json.dumps(kube_config))

            # Load the kubeconfig
            config.load_kube_config(config_file=kubeconfig_path)
        elif kubebox_str:
            kubeconfig_path = "/tmp/kubeconfig"
            os.makedirs(os.path.dirname(kubeconfig_path), exist_ok=True)
            with open(kubeconfig_path, "w") as f:
                f.write(kubebox_str)
            config.load_kube_config(config_file=kubeconfig_path)
        else:
            raise ValueError("No kubeconfig provided.")


if __name__ == "__main__":

    def setup_logging():
        RESET = "\033[0m"
        COLORS = {
            "DEBUG": "\033[34m",  # Blue
            "INFO": "\033[32m",  # Green
            "WARNING": "\033[33m",  # Yellow
            "ERROR": "\033[31m",  # Red
            "CRITICAL": "\033[41m",  # Red background
        }

        class CustomFormatter(logging.Formatter):
            def format(self, record):
                log_fmt = f"{COLORS.get(record.levelname, RESET)}%(asctime)s - %(levelname)s - %(message)s{RESET}"
                formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
                return formatter.format(record)

        # Set up the root logger
        logging.basicConfig(level=logging.INFO)
        logger = logging.getLogger()

        # Remove default handlers and add the custom handler
        if logger.hasHandlers():
            logger.handlers.clear()

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(CustomFormatter())
        logger.addHandler(console_handler)

    async def main(name: str, username: str):
        import os

        from dotenv import load_dotenv

        load_dotenv()
        secret = os.getenv("KUBEBOX_CONFIG")

        KUBEBOX_PUBLIC_KEY = os.getenv("KUBEBOX_PUBLIC_KEY")
        KUBEBOX_PRIVATE_KEY = os.getenv("KUBEBOX_PRIVATE_KEY")

        from kubebox.security import sign_packet, verify_packet

        packet = b"Important data from API"
        signature = sign_packet(packet, KUBEBOX_PRIVATE_KEY)
        is_verified = verify_packet(packet, signature, KUBEBOX_PUBLIC_KEY)
        logger.debug("Packet is verified as coming from the API: %s", is_verified)

        # SANDBOX_PUBLIC_KEY = os.getenv("SANDBOX_PUBLIC_KEY")
        # SANDBOX_PRIVATE_KEY = os.getenv("REMOVE_THIS_SANDBOX_PRIVATE_KEY")
        # encrypted_packet = encrypt_packet(packet, SANDBOX_PUBLIC_KEY)
        # decrypted_packet = decrypt_packet(encrypted_packet, SANDBOX_PRIVATE_KEY)
        # print("Decrypted packet:", decrypted_packet)
        # return

        # kubebox = Kubebox(terraform_path="../../apps/sandbox/terraform.tfstate", print_kubebox_str=True)
        kubebox = Kubebox(secret)
        kubebox.create_secret(
            secret_name="kubebox-public-key",
            namespace="default",
            data={"KUBEBOX_PUBLIC_KEY": KUBEBOX_PUBLIC_KEY},
        )

        pod = kubebox.create_pod(
            name,
            username=username,
            kubebox_public_key_secret_name="kubebox-public-key",
            kubebox_public_key_key="KUBEBOX_PUBLIC_KEY",
        )
        service = kubebox.create_service(name, username=username)

        await pod.wait_until_ready()
        await service.wait_until_ready()
        ip = await service.get_external_ip()
        print(f"http://{ip}")

        # pods = await kubebox.get_all_pods()
        # services = await kubebox.get_all_services()
        # print(pods)
        # print(services)

        # for pod in pods:
        #     await pod.destroy()

        # for service in services:
        #     await service.destroy()

    setup_logging()
    asyncio.run(main("demo-pod", "demo"))
