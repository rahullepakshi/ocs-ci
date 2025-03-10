"""
This module provides base class for different deployment
platforms like AWS, VMWare, Baremetal etc.
"""

from copy import deepcopy
import json
import logging
import os
from subprocess import PIPE, Popen
import tempfile
import time
from pathlib import Path
import base64
import yaml

from ocs_ci.deployment.ocp import OCPDeployment as BaseOCPDeployment
from ocs_ci.deployment.helpers.external_cluster_helpers import (
    ExternalCluster,
    get_external_cluster_client,
)
from ocs_ci.deployment.helpers.mcg_helpers import (
    mcg_only_deployment,
    mcg_only_post_deployment_checks,
)
from ocs_ci.deployment.acm import Submariner
from ocs_ci.deployment.helpers.lso_helpers import setup_local_storage
from ocs_ci.deployment.disconnected import prepare_disconnected_ocs_deployment
from ocs_ci.framework import config, merge_dict
from ocs_ci.ocs import constants, ocp, defaults, registry
from ocs_ci.ocs.cluster import (
    validate_cluster_on_pvc,
    validate_pdb_creation,
    CephClusterExternal,
    get_lvm_full_version,
)
from ocs_ci.ocs.exceptions import (
    CephHealthException,
    CommandFailed,
    PodNotCreated,
    RBDSideCarContainerException,
    ResourceWrongStatusException,
    TimeoutExpiredError,
    UnavailableResourceException,
    UnsupportedFeatureError,
    RDRDeploymentException,
)
from ocs_ci.deployment.zones import create_dummy_zone_labels
from ocs_ci.deployment.netsplit import setup_netsplit
from ocs_ci.ocs.monitoring import (
    create_configmap_cluster_monitoring_pod,
    validate_pvc_created_and_bound_on_monitoring_pods,
    validate_pvc_are_mounted_on_monitoring_pods,
)
from ocs_ci.ocs.node import verify_all_nodes_created
from ocs_ci.ocs.resources import packagemanifest
from ocs_ci.ocs.resources.catalog_source import (
    CatalogSource,
    disable_specific_source,
)
from ocs_ci.ocs.resources.csv import CSV
from ocs_ci.ocs.resources.install_plan import wait_for_install_plan_and_approve
from ocs_ci.ocs.resources.packagemanifest import (
    get_selector_for_ocs_operator,
    PackageManifest,
)
from ocs_ci.ocs.resources.pod import (
    get_all_pods,
    validate_pods_are_respinned_and_running_state,
    get_pods_having_label,
    get_pod_count,
)
from ocs_ci.ocs.resources.storage_cluster import (
    ocs_install_verification,
    setup_ceph_debug,
)
from ocs_ci.ocs.uninstall import uninstall_ocs
from ocs_ci.ocs.utils import (
    get_non_acm_cluster_config,
    get_primary_cluster_config,
    setup_ceph_toolbox,
    collect_ocs_logs,
    enable_console_plugin,
)
from ocs_ci.utility.deployment import create_external_secret
from ocs_ci.utility.flexy import load_cluster_info
from ocs_ci.utility import (
    templating,
    ibmcloud,
    kms as KMS,
    version,
)
from ocs_ci.utility.retry import retry
from ocs_ci.utility.secret import link_all_sa_and_secret_and_delete_pods
from ocs_ci.utility.ssl_certs import configure_custom_ingress_cert
from ocs_ci.utility.utils import (
    ceph_health_check,
    clone_repo,
    enable_huge_pages,
    exec_cmd,
    get_latest_ds_olm_tag,
    is_cluster_running,
    run_cmd,
    run_cmd_multicluster,
    set_selinux_permissions,
    set_registry_to_managed_state,
    add_stage_cert,
    modify_csv,
    wait_for_machineconfigpool_status,
    load_auth_config,
    TimeoutSampler,
)
from ocs_ci.utility.vsphere_nodes import update_ntp_compute_nodes
from ocs_ci.helpers import helpers
from ocs_ci.helpers.helpers import (
    set_configmap_log_level_rook_ceph_operator,
)
from ocs_ci.ocs.ui.helpers_ui import ui_deployment_conditions
from ocs_ci.utility.utils import get_az_count
from ocs_ci.utility.ibmcloud import run_ibmcloud_cmd

logger = logging.getLogger(__name__)


class Deployment(object):
    """
    Base for all deployment platforms
    """

    # Default storage class for StorageCluster CRD,
    # every platform specific class which extending this base class should
    # define it
    DEFAULT_STORAGECLASS = None

    # Default storage class for LSO deployments. While each platform specific
    # subclass can redefine it, there is a well established platform
    # independent default value (based on OCS Installation guide), and it's
    # redefinition is not necessary in normal cases.
    DEFAULT_STORAGECLASS_LSO = "localblock"

    CUSTOM_STORAGE_CLASS_PATH = None
    """str: filepath of yaml file with custom storage class if necessary

    For some platforms, one have to create custom storage class for OCS to make
    sure ceph uses disks of expected type and parameters (eg. OCS requires
    ssd). This variable is either None (meaning that such custom storage class
    is not needed), or point to a yaml file with custom storage class.
    """

    def __init__(self):
        self.platform = config.ENV_DATA["platform"]
        self.ocp_deployment_type = config.ENV_DATA["deployment_type"]
        self.cluster_path = config.ENV_DATA["cluster_path"]
        self.namespace = config.ENV_DATA["cluster_namespace"]

    class OCPDeployment(BaseOCPDeployment):
        """
        This class has to be implemented in child class and should overload
        methods for platform specific config.
        """

        pass

    def do_deploy_ocp(self, log_cli_level):
        """
        Deploy OCP
        Args:
            log_cli_level (str): log level for the installer

        """
        if not config.ENV_DATA["skip_ocp_deployment"]:
            if is_cluster_running(self.cluster_path):
                logger.warning("OCP cluster is already running, skipping installation")
            else:
                try:
                    self.deploy_ocp(log_cli_level)
                    self.post_ocp_deploy()
                except Exception as e:
                    config.RUN["is_ocp_deployment_failed"] = True
                    logger.error(e)
                    if config.REPORTING["gather_on_deploy_failure"]:
                        collect_ocs_logs("deployment", ocs=False)
                    raise

    def do_deploy_submariner(self):
        """
        Deploy Submariner operator

        """
        if config.ENV_DATA.get("skip_submariner_deployment", False):
            return

        # Multicluster operations
        if config.multicluster:
            # Configure submariner only on non-ACM clusters
            submariner = Submariner()
            submariner.deploy()

    def do_deploy_ocs(self):
        """
        Deploy OCS/ODF and run verification as well

        """
        if not config.ENV_DATA["skip_ocs_deployment"]:
            for i in range(config.nclusters):
                if config.multicluster and config.get_acm_index() == i:
                    continue
                config.switch_ctx(i)
                try:
                    self.deploy_ocs()

                    if config.REPORTING["collect_logs_on_success_run"]:
                        collect_ocs_logs("deployment", ocp=False, status_failure=False)
                except Exception as e:
                    logger.error(e)
                    if config.REPORTING["gather_on_deploy_failure"]:
                        # Let's do the collections separately to guard against one
                        # of them failing
                        collect_ocs_logs("deployment", ocs=False)
                        collect_ocs_logs("deployment", ocp=False)
                    raise
            config.reset_ctx()
            # Run ocs_install_verification here only in case of multicluster.
            # For single cluster, test_deployment will take care.
            if config.multicluster:
                for i in range(config.multicluster):
                    if config.get_acm_index() == i:
                        continue
                    else:
                        config.switch_ctx(i)
                        ocs_registry_image = config.DEPLOYMENT.get(
                            "ocs_registry_image", None
                        )
                        ocs_install_verification(ocs_registry_image=ocs_registry_image)
                config.reset_ctx()
        else:
            logger.warning("OCS deployment will be skipped")

    def do_deploy_rdr(self):
        """
        Call Regional DR deploy

        """
        # Multicluster: Handle all ODF multicluster DR ops
        if config.multicluster:
            dr_conf = self.get_rdr_conf()
            deploy_dr = MultiClusterDROperatorsDeploy(dr_conf)
            deploy_dr.deploy()

    def do_deploy_lvmo(self):
        """
        call lvm deploy

        """
        self.deploy_lvmo()

    def deploy_cluster(self, log_cli_level="DEBUG"):
        """
        We are handling both OCP and OCS deployment here based on flags

        Args:
            log_cli_level (str): log level for installer (default: DEBUG)
        """
        self.do_deploy_ocp(log_cli_level)
        # Deployment of network split scripts via machineconfig API happens
        # before OCS deployment.
        if config.DEPLOYMENT.get("network_split_setup"):
            master_zones = config.ENV_DATA.get("master_availability_zones")
            worker_zones = config.ENV_DATA.get("worker_availability_zones")
            # special external zone, which is directly defined by ip addr list,
            # such zone could represent external services, which we could block
            # access to via ax-bx-cx network split
            if config.DEPLOYMENT.get("network_split_zonex_addrs") is not None:
                x_addr_list = config.DEPLOYMENT["network_split_zonex_addrs"].split(",")
            else:
                x_addr_list = None
            if config.DEPLOYMENT.get("arbiter_deployment"):
                arbiter_zone = self.get_arbiter_location()
                logger.debug("detected arbiter zone: %s", arbiter_zone)
            else:
                arbiter_zone = None
            # TODO: use temporary directory for all temporary files of
            # ocs-deployment, not just here in this particular case
            tmp_path = Path(tempfile.mkdtemp(prefix="ocs-ci-deployment-"))
            logger.debug("created temporary directory %s", tmp_path)
            setup_netsplit(
                tmp_path, master_zones, worker_zones, x_addr_list, arbiter_zone
            )
        ocp_version = version.get_semantic_ocp_version_from_config()
        if (
            config.ENV_DATA.get("deploy_acm_hub_cluster")
            and ocp_version >= version.VERSION_4_9
        ):
            self.deploy_acm_hub()
        self.do_deploy_lvmo()
        self.do_deploy_submariner()
        self.do_deploy_ocs()
        self.do_deploy_rdr()

    def get_rdr_conf(self):
        """
        Aggregate important Regional DR parameters in the dictionary

        Returns:
            dict: of Regional DR config parameters

        """
        dr_conf = dict()
        dr_conf["rbd_dr_scenario"] = config.ENV_DATA.get("rbd_dr_scenario", False)
        dr_conf["dr_metadata_store"] = config.ENV_DATA.get("dr_metadata_store", "awss3")
        return dr_conf

    def deploy_ocp(self, log_cli_level="DEBUG"):
        """
        Base deployment steps, the rest should be implemented in the child
        class.

        Args:
            log_cli_level (str): log level for installer (default: DEBUG)
        """
        self.ocp_deployment = self.OCPDeployment()
        self.ocp_deployment.deploy_prereq()
        self.ocp_deployment.deploy(log_cli_level)
        # logging the cluster UUID so that we can ask for it's telemetry data
        cluster_id = run_cmd(
            "oc get clusterversion version -o jsonpath='{.spec.clusterID}'"
        )
        logger.info(f"clusterID (UUID): {cluster_id}")

    def post_ocp_deploy(self):
        """
        Function does post OCP deployment stuff we need to do.
        """
        if config.DEPLOYMENT.get("use_custom_ingress_ssl_cert"):
            configure_custom_ingress_cert()
        verify_all_nodes_created()
        set_selinux_permissions()
        set_registry_to_managed_state()
        add_stage_cert()
        if config.ENV_DATA.get("huge_pages"):
            enable_huge_pages()
        if config.DEPLOYMENT.get("dummy_zone_node_labels"):
            create_dummy_zone_labels()

    def label_and_taint_nodes(self):
        """
        Label and taint worker nodes to be used by OCS operator
        """

        # TODO: remove this "heuristics", it doesn't belong there, the process
        # should be explicit and simple, this is asking for trouble, bugs and
        # silently invalid deployments ...
        # See https://github.com/red-hat-storage/ocs-ci/issues/4470
        arbiter_deployment = config.DEPLOYMENT.get("arbiter_deployment")

        nodes = ocp.OCP(kind="node").get().get("items", [])

        worker_nodes = [
            node
            for node in nodes
            if constants.WORKER_LABEL in node["metadata"]["labels"]
        ]
        if not worker_nodes:
            raise UnavailableResourceException("No worker node found!")
        az_worker_nodes = {}
        for node in worker_nodes:
            az = node["metadata"]["labels"].get(constants.ZONE_LABEL)
            az_node_list = az_worker_nodes.get(az, [])
            az_node_list.append(node["metadata"]["name"])
            az_worker_nodes[az] = az_node_list
        logger.debug(f"Found the worker nodes in AZ: {az_worker_nodes}")

        if arbiter_deployment:
            to_label = config.DEPLOYMENT.get("ocs_operator_nodes_to_label", 4)
        else:
            to_label = config.DEPLOYMENT.get("ocs_operator_nodes_to_label")

        distributed_worker_nodes = []
        if arbiter_deployment and config.DEPLOYMENT.get("arbiter_autodetect"):
            for az in list(az_worker_nodes.keys()):
                az_node_list = az_worker_nodes.get(az)
                if az_node_list and len(az_node_list) > 1:
                    node_names = az_node_list[:2]
                    distributed_worker_nodes += node_names
        elif arbiter_deployment and not config.DEPLOYMENT.get("arbiter_autodetect"):
            to_label_per_az = int(
                to_label / len(config.ENV_DATA.get("worker_availability_zones"))
            )
            for az in list(config.ENV_DATA.get("worker_availability_zones")):
                az_node_list = az_worker_nodes.get(az)
                if az_node_list and len(az_node_list) > 1:
                    node_names = az_node_list[:to_label_per_az]
                    distributed_worker_nodes += node_names
                else:
                    raise UnavailableResourceException(
                        "Atleast 2 worker nodes required for arbiter cluster in zone %s",
                        az,
                    )
        else:
            while az_worker_nodes:
                for az in list(az_worker_nodes.keys()):
                    az_node_list = az_worker_nodes.get(az)
                    if az_node_list:
                        node_name = az_node_list.pop(0)
                        distributed_worker_nodes.append(node_name)
                    else:
                        del az_worker_nodes[az]
        logger.info(f"Distributed worker nodes for AZ: {distributed_worker_nodes}")

        to_taint = config.DEPLOYMENT.get("ocs_operator_nodes_to_taint", 0)

        distributed_worker_count = len(distributed_worker_nodes)
        if distributed_worker_count < to_label or distributed_worker_count < to_taint:
            logger.info(f"All nodes: {nodes}")
            logger.info(f"Distributed worker nodes: {distributed_worker_nodes}")
            raise UnavailableResourceException(
                f"Not enough distributed worker nodes: {distributed_worker_count} to label: "
                f"{to_label} or taint: {to_taint}!"
            )

        _ocp = ocp.OCP(kind="node")
        workers_to_label = " ".join(distributed_worker_nodes[:to_label])
        if workers_to_label:

            logger.info(
                f"Label nodes: {workers_to_label} with label: "
                f"{constants.OPERATOR_NODE_LABEL}"
            )
            label_cmds = [
                (
                    f"label nodes {workers_to_label} "
                    f"{constants.OPERATOR_NODE_LABEL} --overwrite"
                )
            ]
            if config.DEPLOYMENT.get("infra_nodes") and not config.ENV_DATA.get(
                "infra_replicas"
            ):
                logger.info(
                    f"Label nodes: {workers_to_label} with label: "
                    f"{constants.INFRA_NODE_LABEL}"
                )
                label_cmds.append(
                    f"label nodes {workers_to_label} "
                    f"{constants.INFRA_NODE_LABEL} --overwrite"
                )

            for cmd in label_cmds:
                _ocp.exec_oc_cmd(command=cmd)

        workers_to_taint = " ".join(distributed_worker_nodes[:to_taint])
        if workers_to_taint:
            logger.info(
                f"Taint nodes: {workers_to_taint} with taint: "
                f"{constants.OPERATOR_NODE_TAINT}"
            )
            taint_cmd = (
                f"adm taint nodes {workers_to_taint} {constants.OPERATOR_NODE_TAINT}"
            )
            _ocp.exec_oc_cmd(command=taint_cmd)

    def subscribe_ocs(self):
        """
        This method subscription manifest and subscribe to OCS operator.

        """
        live_deployment = config.DEPLOYMENT.get("live_deployment")
        if (
            config.ENV_DATA["platform"] == constants.IBMCLOUD_PLATFORM
            and not live_deployment
        ):
            link_all_sa_and_secret_and_delete_pods(constants.OCS_SECRET, self.namespace)
        operator_selector = get_selector_for_ocs_operator()
        # wait for package manifest
        # For OCS version >= 4.9, we have odf-operator
        ocs_version = version.get_semantic_ocs_version_from_config()
        if ocs_version >= version.VERSION_4_9:
            ocs_operator_name = defaults.ODF_OPERATOR_NAME
            subscription_file = constants.SUBSCRIPTION_ODF_YAML
        else:
            ocs_operator_name = defaults.OCS_OPERATOR_NAME
            subscription_file = constants.SUBSCRIPTION_YAML

        package_manifest = PackageManifest(
            resource_name=ocs_operator_name,
            selector=operator_selector,
        )
        # Wait for package manifest is ready
        package_manifest.wait_for_resource(timeout=300)
        default_channel = package_manifest.get_default_channel()
        subscription_yaml_data = templating.load_yaml(subscription_file)
        subscription_plan_approval = config.DEPLOYMENT.get("subscription_plan_approval")
        if subscription_plan_approval:
            subscription_yaml_data["spec"][
                "installPlanApproval"
            ] = subscription_plan_approval
        custom_channel = config.DEPLOYMENT.get("ocs_csv_channel")
        if custom_channel:
            logger.info(f"Custom channel will be used: {custom_channel}")
            subscription_yaml_data["spec"]["channel"] = custom_channel
        else:
            logger.info(f"Default channel will be used: {default_channel}")
            subscription_yaml_data["spec"]["channel"] = default_channel
        if config.DEPLOYMENT.get("stage"):
            subscription_yaml_data["spec"]["source"] = constants.OPERATOR_SOURCE_NAME
        if config.DEPLOYMENT.get("live_deployment"):
            subscription_yaml_data["spec"]["source"] = config.DEPLOYMENT.get(
                "live_content_source", defaults.LIVE_CONTENT_SOURCE
            )
        subscription_manifest = tempfile.NamedTemporaryFile(
            mode="w+", prefix="subscription_manifest", delete=False
        )
        templating.dump_data_to_temp_yaml(
            subscription_yaml_data, subscription_manifest.name
        )
        run_cmd(f"oc create -f {subscription_manifest.name}")
        self.wait_for_subscription(ocs_operator_name)
        if subscription_plan_approval == "Manual":
            wait_for_install_plan_and_approve(self.namespace)
            csv_name = package_manifest.get_current_csv(channel=custom_channel)
            csv = CSV(resource_name=csv_name, namespace=self.namespace)
            csv.wait_for_phase("Installing", timeout=60)
        self.wait_for_csv(ocs_operator_name)
        logger.info("Sleeping for 30 seconds after CSV created")
        time.sleep(30)

    def wait_for_subscription(self, subscription_name):
        """
        Wait for the subscription to appear

        Args:
            subscription_name (str): Subscription name pattern

        """
        ocp.OCP(kind="subscription", namespace=self.namespace)
        for sample in TimeoutSampler(
            300, 10, ocp.OCP, kind="subscription", namespace=self.namespace
        ):
            subscriptions = sample.get().get("items", [])
            for subscription in subscriptions:
                found_subscription_name = subscription.get("metadata", {}).get(
                    "name", ""
                )
                if subscription_name in found_subscription_name:
                    logger.info(f"Subscription found: {found_subscription_name}")
                    return
                logger.debug(f"Still waiting for the subscription: {subscription_name}")

    def wait_for_csv(self, csv_name):
        """
        Wait for the CSV to appear

        Args:
            csv_name (str): CSV name pattern

        """
        ocp.OCP(kind="subscription", namespace=self.namespace)
        for sample in TimeoutSampler(
            300, 10, ocp.OCP, kind="csv", namespace=self.namespace
        ):
            csvs = sample.get().get("items", [])
            for csv in csvs:
                found_csv_name = csv.get("metadata", {}).get("name", "")
                if csv_name in found_csv_name:
                    logger.info(f"CSV found: {found_csv_name}")
                    return
                logger.debug(f"Still waiting for the CSV: {csv_name}")

    def get_arbiter_location(self):
        """
        Get arbiter mon location for storage cluster
        """
        if config.DEPLOYMENT.get("arbiter_deployment") and not config.DEPLOYMENT.get(
            "arbiter_autodetect"
        ):
            return config.DEPLOYMENT.get("arbiter_zone")

        # below logic will autodetect arbiter_zone
        nodes = ocp.OCP(kind="node").get().get("items", [])

        worker_nodes_zones = {
            node["metadata"]["labels"].get(constants.ZONE_LABEL)
            for node in nodes
            if constants.WORKER_LABEL in node["metadata"]["labels"]
            and str(constants.OPERATOR_NODE_LABEL)[:-3] in node["metadata"]["labels"]
        }

        master_nodes_zones = {
            node["metadata"]["labels"].get(constants.ZONE_LABEL)
            for node in nodes
            if constants.MASTER_LABEL in node["metadata"]["labels"]
        }

        arbiter_locations = list(master_nodes_zones - worker_nodes_zones)

        if len(arbiter_locations) < 1:
            raise UnavailableResourceException(
                "Atleast 1 different zone required than storage nodes in master nodes to host arbiter mon"
            )

        return arbiter_locations[0]

    def deploy_ocs_via_operator(self, image=None):
        """
        Method for deploy OCS via OCS operator

        Args:
            image (str): Image of ocs registry.

        """
        ui_deployment = config.DEPLOYMENT.get("ui_deployment")
        live_deployment = config.DEPLOYMENT.get("live_deployment")
        arbiter_deployment = config.DEPLOYMENT.get("arbiter_deployment")

        if ui_deployment and ui_deployment_conditions():
            self.deployment_with_ui()
            # Skip the rest of the deployment when deploy via UI
            return
        else:
            logger.info("Deployment of OCS via OCS operator")
            self.label_and_taint_nodes()

        if not live_deployment:
            create_catalog_source(image)

        if config.DEPLOYMENT.get("local_storage"):
            setup_local_storage(storageclass=self.DEFAULT_STORAGECLASS_LSO)

        logger.info("Creating namespace and operator group.")
        run_cmd(f"oc create -f {constants.OLM_YAML}")

        # create multus network
        if config.ENV_DATA.get("is_multus_enabled"):
            logger.info("Creating multus network")
            multus_data = templating.load_yaml(constants.MULTUS_YAML)
            multus_config_str = multus_data["spec"]["config"]
            multus_config_dct = json.loads(multus_config_str)
            if config.ENV_DATA.get("multus_public_network_interface"):
                multus_config_dct["master"] = config.ENV_DATA.get(
                    "multus_public_network_interface"
                )
            multus_data["spec"]["config"] = json.dumps(multus_config_dct)
            multus_data_yaml = tempfile.NamedTemporaryFile(
                mode="w+", prefix="multus", delete=False
            )
            templating.dump_data_to_temp_yaml(multus_data, multus_data_yaml.name)
            run_cmd(f"oc create -f {multus_data_yaml.name}")

        disable_addon = config.DEPLOYMENT.get("ibmcloud_disable_addon")

        if config.ENV_DATA["platform"] == constants.IBMCLOUD_PLATFORM:
            ibmcloud.add_deployment_dependencies()
            if not live_deployment:
                create_ocs_secret(self.namespace)
            if config.DEPLOYMENT.get("create_ibm_cos_secret", True):
                logger.info("Creating secret for IBM Cloud Object Storage")
                with open(constants.IBM_COS_SECRET_YAML, "r") as cos_secret_fd:
                    cos_secret_data = yaml.load(cos_secret_fd, Loader=yaml.SafeLoader)
                key_id = config.AUTH["ibmcloud"]["ibm_cos_access_key_id"]
                key_secret = config.AUTH["ibmcloud"]["ibm_cos_secret_access_key"]
                cos_secret_data["data"]["IBM_COS_ACCESS_KEY_ID"] = key_id
                cos_secret_data["data"]["IBM_COS_SECRET_ACCESS_KEY"] = key_secret
                cos_secret_data_yaml = tempfile.NamedTemporaryFile(
                    mode="w+", prefix="cos_secret", delete=False
                )
                templating.dump_data_to_temp_yaml(
                    cos_secret_data, cos_secret_data_yaml.name
                )
                exec_cmd(f"oc create -f {cos_secret_data_yaml.name}")
        if (
            config.ENV_DATA["platform"] == constants.IBMCLOUD_PLATFORM
            and live_deployment
            and not disable_addon
        ):
            self.deploy_odf_addon()
            return
        self.subscribe_ocs()
        operator_selector = get_selector_for_ocs_operator()
        subscription_plan_approval = config.DEPLOYMENT.get("subscription_plan_approval")
        ocs_version = version.get_semantic_ocs_version_from_config()
        if ocs_version >= version.VERSION_4_9:
            ocs_operator_names = [
                defaults.ODF_OPERATOR_NAME,
                defaults.OCS_OPERATOR_NAME,
                defaults.MCG_OPERATOR,
            ]
            # workaround for https://bugzilla.redhat.com/show_bug.cgi?id=2075422
            ocp_version = version.get_semantic_ocp_version_from_config()
            if (
                live_deployment
                and ocp_version == version.VERSION_4_10
                and ocs_version == version.VERSION_4_9
            ):
                ocs_operator_names.remove(defaults.MCG_OPERATOR)
        else:
            ocs_operator_names = [defaults.OCS_OPERATOR_NAME]

        if ocs_version >= version.VERSION_4_10:
            ocs_operator_names.append(defaults.ODF_CSI_ADDONS_OPERATOR)

        channel = config.DEPLOYMENT.get("ocs_csv_channel")
        is_ibm_sa_linked = False

        for ocs_operator_name in ocs_operator_names:
            package_manifest = PackageManifest(
                resource_name=ocs_operator_name,
                selector=operator_selector,
                subscription_plan_approval=subscription_plan_approval,
            )
            package_manifest.wait_for_resource(timeout=300)
            csv_name = package_manifest.get_current_csv(channel=channel)
            csv = CSV(resource_name=csv_name, namespace=self.namespace)
            if (
                config.ENV_DATA["platform"] == constants.IBMCLOUD_PLATFORM
                and not live_deployment
            ):
                if not is_ibm_sa_linked:
                    logger.info("Sleeping for 60 seconds before applying SA")
                    time.sleep(60)
                    link_all_sa_and_secret_and_delete_pods(
                        constants.OCS_SECRET, self.namespace
                    )
                    is_ibm_sa_linked = True
            csv.wait_for_phase("Succeeded", timeout=720)
        # create storage system
        if ocs_version >= version.VERSION_4_9:
            exec_cmd(f"oc apply -f {constants.STORAGE_SYSTEM_ODF_YAML}")

        ocp_version = version.get_semantic_ocp_version_from_config()
        if config.ENV_DATA["platform"] == constants.IBMCLOUD_PLATFORM:
            config_map = ocp.OCP(
                kind="configmap",
                namespace=self.namespace,
                resource_name=constants.ROOK_OPERATOR_CONFIGMAP,
            )
            config_map.get(retry=10, wait=5)
            config_map_patch = (
                '\'{"data": {"ROOK_CSI_KUBELET_DIR_PATH": "/var/data/kubelet"}}\''
            )
            logger.info("Patching config map to change KUBLET DIR PATH")
            exec_cmd(
                f"oc patch configmap -n {self.namespace} "
                f"{constants.ROOK_OPERATOR_CONFIGMAP} -p {config_map_patch}"
            )

        # Modify the CSV with custom values if required
        if all(
            key in config.DEPLOYMENT for key in ("csv_change_from", "csv_change_to")
        ):
            modify_csv(
                csv=csv_name,
                replace_from=config.DEPLOYMENT["csv_change_from"],
                replace_to=config.DEPLOYMENT["csv_change_to"],
            )

        # create custom storage class for StorageCluster CR if necessary
        if self.CUSTOM_STORAGE_CLASS_PATH is not None:
            with open(self.CUSTOM_STORAGE_CLASS_PATH, "r") as custom_sc_fo:
                custom_sc = yaml.load(custom_sc_fo, Loader=yaml.SafeLoader)
            # set value of DEFAULT_STORAGECLASS to mach the custom storage cls
            self.DEFAULT_STORAGECLASS = custom_sc["metadata"]["name"]
            run_cmd(f"oc create -f {self.CUSTOM_STORAGE_CLASS_PATH}")

        # Set rook log level
        self.set_rook_log_level()

        # creating StorageCluster
        if config.DEPLOYMENT.get("kms_deployment"):
            kms = KMS.get_kms_deployment()
            kms.deploy()

        if config.ENV_DATA["mcg_only_deployment"]:
            mcg_only_deployment()
            return

        cluster_data = templating.load_yaml(constants.STORAGE_CLUSTER_YAML)
        # Figure out all the OCS modules enabled/disabled
        # CLI parameter --disable-components takes the precedence over
        # anything which comes from config file
        if config.ENV_DATA.get("disable_components"):
            for component in config.ENV_DATA["disable_components"]:
                config.COMPONENTS[f"disable_{component}"] = True
                logger.warning(f"disabling: {component}")

        # Update cluster_data with respective component enable/disable
        for key in config.COMPONENTS.keys():
            comp_name = constants.OCS_COMPONENTS_MAP[key.split("_")[1]]
            if config.COMPONENTS[key]:
                if "noobaa" in key:
                    merge_dict(
                        cluster_data,
                        {
                            "spec": {
                                "multiCloudGateway": {"reconcileStrategy": "ignore"}
                            }
                        },
                    )
                else:
                    merge_dict(
                        cluster_data,
                        {
                            "spec": {
                                "managedResources": {
                                    f"{comp_name}": {"reconcileStrategy": "ignore"}
                                }
                            }
                        },
                    )

        if arbiter_deployment:
            cluster_data["spec"]["arbiter"] = {}
            cluster_data["spec"]["nodeTopologies"] = {}
            cluster_data["spec"]["arbiter"]["enable"] = True
            cluster_data["spec"]["nodeTopologies"][
                "arbiterLocation"
            ] = self.get_arbiter_location()
            cluster_data["spec"]["storageDeviceSets"][0]["replica"] = 4

        cluster_data["metadata"]["name"] = config.ENV_DATA["storage_cluster_name"]

        deviceset_data = cluster_data["spec"]["storageDeviceSets"][0]
        device_size = int(config.ENV_DATA.get("device_size", defaults.DEVICE_SIZE))

        logger.info(
            "Flexible scaling is available from version 4.7 on LSO cluster with less than 3 zones"
        )
        zone_num = get_az_count()
        if (
            config.DEPLOYMENT.get("local_storage")
            and ocs_version >= version.VERSION_4_7
            and zone_num < 3
            and not config.DEPLOYMENT.get("arbiter_deployment")
        ):
            cluster_data["spec"]["flexibleScaling"] = True
            # https://bugzilla.redhat.com/show_bug.cgi?id=1921023
            cluster_data["spec"]["storageDeviceSets"][0]["count"] = 3
            cluster_data["spec"]["storageDeviceSets"][0]["replica"] = 1

        # set size of request for storage
        if self.platform.lower() == constants.BAREMETAL_PLATFORM:
            pv_size_list = helpers.get_pv_size(
                storageclass=self.DEFAULT_STORAGECLASS_LSO
            )
            pv_size_list.sort()
            deviceset_data["dataPVCTemplate"]["spec"]["resources"]["requests"][
                "storage"
            ] = f"{pv_size_list[0]}"
        else:
            deviceset_data["dataPVCTemplate"]["spec"]["resources"]["requests"][
                "storage"
            ] = f"{device_size}Gi"

        # set storage class to OCS default on current platform
        if self.DEFAULT_STORAGECLASS:
            deviceset_data["dataPVCTemplate"]["spec"][
                "storageClassName"
            ] = self.DEFAULT_STORAGECLASS

        # StorageCluster tweaks for LSO
        if config.DEPLOYMENT.get("local_storage"):
            cluster_data["spec"]["manageNodes"] = False
            cluster_data["spec"]["monDataDirHostPath"] = "/var/lib/rook"
            deviceset_data["name"] = constants.DEFAULT_DEVICESET_LSO_PVC_NAME
            deviceset_data["portable"] = False
            deviceset_data["dataPVCTemplate"]["spec"][
                "storageClassName"
            ] = self.DEFAULT_STORAGECLASS_LSO
            lso_type = config.DEPLOYMENT.get("type")
            if (
                self.platform.lower() == constants.AWS_PLATFORM
                and not lso_type == constants.AWS_EBS
            ):
                deviceset_data["count"] = 2
            # setting resource limits for AWS i3
            # https://access.redhat.com/documentation/en-us/red_hat_openshift_container_storage/4.6/html-single/deploying_openshift_container_storage_using_amazon_web_services/index#creating-openshift-container-storage-cluster-on-amazon-ec2_local-storage
            if (
                ocs_version >= version.VERSION_4_5
                and config.ENV_DATA.get("worker_instance_type")
                == constants.AWS_LSO_WORKER_INSTANCE
            ):
                deviceset_data["resources"] = {
                    "limits": {"cpu": 2, "memory": "5Gi"},
                    "requests": {"cpu": 1, "memory": "5Gi"},
                }
            if (ocp_version >= version.VERSION_4_6) and (
                ocs_version >= version.VERSION_4_6
            ):
                cluster_data["metadata"]["annotations"] = {
                    "cluster.ocs.openshift.io/local-devices": "true"
                }
            count = config.DEPLOYMENT.get("local_storage_storagedeviceset_count")
            if count is not None:
                deviceset_data["count"] = count

        # Allow lower instance requests and limits for OCS deployment
        # The resources we need to change can be found here:
        # https://github.com/openshift/ocs-operator/blob/release-4.5/pkg/deploy-manager/storagecluster.go#L88-L116
        if config.DEPLOYMENT.get("allow_lower_instance_requirements"):
            none_resources = {"Requests": None, "Limits": None}
            deviceset_data["resources"] = deepcopy(none_resources)
            resources = [
                "mon",
                "mds",
                "rgw",
                "mgr",
                "noobaa-core",
                "noobaa-db",
            ]
            if ocs_version >= version.VERSION_4_5:
                resources.append("noobaa-endpoint")
            cluster_data["spec"]["resources"] = {
                resource: deepcopy(none_resources) for resource in resources
            }
            if ocs_version >= version.VERSION_4_5:
                cluster_data["spec"]["resources"]["noobaa-endpoint"] = {
                    "limits": {"cpu": 1, "memory": "500Mi"},
                    "requests": {"cpu": 1, "memory": "500Mi"},
                }
        else:
            local_storage = config.DEPLOYMENT.get("local_storage")
            platform = config.ENV_DATA.get("platform", "").lower()
            if local_storage and platform == "aws":
                resources = {
                    "mds": {
                        "limits": {"cpu": 3, "memory": "8Gi"},
                        "requests": {"cpu": 1, "memory": "8Gi"},
                    }
                }
                if ocs_version < version.VERSION_4_5:
                    resources["noobaa-core"] = {
                        "limits": {"cpu": 2, "memory": "8Gi"},
                        "requests": {"cpu": 1, "memory": "8Gi"},
                    }
                    resources["noobaa-db"] = {
                        "limits": {"cpu": 2, "memory": "8Gi"},
                        "requests": {"cpu": 1, "memory": "8Gi"},
                    }
                cluster_data["spec"]["resources"] = resources

        # Enable host network if enabled in config (this require all the
        # rules to be enabled on underlaying platform).
        if config.DEPLOYMENT.get("host_network"):
            cluster_data["spec"]["hostNetwork"] = True

        cluster_data["spec"]["storageDeviceSets"] = [deviceset_data]

        if self.platform == constants.IBMCLOUD_PLATFORM:
            mon_pvc_template = {
                "spec": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "20Gi"}},
                    "storageClassName": self.DEFAULT_STORAGECLASS,
                    "volumeMode": "Filesystem",
                }
            }
            cluster_data["spec"]["monPVCTemplate"] = mon_pvc_template
            # Need to check if it's needed for ibm cloud to set manageNodes
            cluster_data["spec"]["manageNodes"] = False

        if config.ENV_DATA.get("encryption_at_rest"):
            if ocs_version < version.VERSION_4_6:
                error_message = "Encryption at REST can be enabled only on OCS >= 4.6!"
                logger.error(error_message)
                raise UnsupportedFeatureError(error_message)
            logger.info("Enabling encryption at REST!")
            cluster_data["spec"]["encryption"] = {
                "enable": True,
            }
            if ocs_version >= version.VERSION_4_10:
                cluster_data["spec"]["encryption"] = {
                    "clusterWide": True,
                }
            if config.DEPLOYMENT.get("kms_deployment"):
                cluster_data["spec"]["encryption"]["kms"] = {
                    "enable": True,
                }

        if config.DEPLOYMENT.get("ceph_debug"):
            setup_ceph_debug()
            cluster_data["spec"]["managedResources"] = {
                "cephConfig": {"reconcileStrategy": "ignore"}
            }
        if config.ENV_DATA.get("is_multus_enabled"):
            cluster_data["spec"]["network"] = {
                "provider": "multus",
                "selectors": {
                    "public": f"{defaults.ROOK_CLUSTER_NAMESPACE}/ocs-public"
                },
            }

        cluster_data_yaml = tempfile.NamedTemporaryFile(
            mode="w+", prefix="cluster_storage", delete=False
        )
        templating.dump_data_to_temp_yaml(cluster_data, cluster_data_yaml.name)
        run_cmd(f"oc create -f {cluster_data_yaml.name}", timeout=1200)
        if config.DEPLOYMENT["infra_nodes"]:
            _ocp = ocp.OCP(kind="node")
            _ocp.exec_oc_cmd(
                command=f"annotate namespace {defaults.ROOK_CLUSTER_NAMESPACE} "
                f"{constants.NODE_SELECTOR_ANNOTATION}"
            )

    def deploy_odf_addon(self):
        """
        This method deploy ODF addon.

        """
        logger.info("Deploying odf with ocs addon.")
        clustername = config.ENV_DATA.get("cluster_name")
        ocs_version = version.get_semantic_ocs_version_from_config()
        cmd = (
            f"ibmcloud ks cluster addon enable openshift-data-foundation --cluster {clustername} -f --version "
            f"{ocs_version}.0"
        )
        run_ibmcloud_cmd(cmd)
        time.sleep(120)
        logger.info("Ocs addon started enabling.")

    def deployment_with_ui(self):
        """
        Deployment OCS Operator via OpenShift Console

        """
        from ocs_ci.ocs.ui.base_ui import login_ui, close_browser
        from ocs_ci.ocs.ui.deployment_ui import DeploymentUI

        create_catalog_source()
        setup_ui = login_ui()
        deployment_obj = DeploymentUI(setup_ui)
        deployment_obj.install_ocs_ui()
        close_browser(setup_ui)

    def deploy_with_external_mode(self):
        """
        This function handles the deployment of OCS on
        external/indpendent RHCS cluster

        """
        live_deployment = config.DEPLOYMENT.get("live_deployment")
        logger.info("Deploying OCS with external mode RHCS")
        ui_deployment = config.DEPLOYMENT.get("ui_deployment")
        if not ui_deployment:
            logger.info("Creating namespace and operator group.")
            run_cmd(f"oc create -f {constants.OLM_YAML}")
        if not live_deployment:
            create_catalog_source()
        self.subscribe_ocs()
        operator_selector = get_selector_for_ocs_operator()
        subscription_plan_approval = config.DEPLOYMENT.get("subscription_plan_approval")
        ocs_version = version.get_semantic_ocs_version_from_config()
        if ocs_version >= version.VERSION_4_9:
            ocs_operator_names = [
                defaults.ODF_OPERATOR_NAME,
                defaults.OCS_OPERATOR_NAME,
            ]
        else:
            ocs_operator_names = [defaults.OCS_OPERATOR_NAME]
        channel = config.DEPLOYMENT.get("ocs_csv_channel")
        for ocs_operator_name in ocs_operator_names:
            package_manifest = PackageManifest(
                resource_name=ocs_operator_name,
                selector=operator_selector,
                subscription_plan_approval=subscription_plan_approval,
            )
            package_manifest.wait_for_resource(timeout=300)
            csv_name = package_manifest.get_current_csv(channel=channel)
            csv = CSV(resource_name=csv_name, namespace=self.namespace)
            csv.wait_for_phase("Succeeded", timeout=720)

        # Set rook log level
        self.set_rook_log_level()

        # get external cluster details
        host, user, password = get_external_cluster_client()
        external_cluster = ExternalCluster(host, user, password)
        external_cluster.get_external_cluster_details()

        # get admin keyring
        external_cluster.get_admin_keyring()

        # Create secret for external cluster
        create_external_secret()

        cluster_data = templating.load_yaml(constants.EXTERNAL_STORAGE_CLUSTER_YAML)
        cluster_data["metadata"]["name"] = config.ENV_DATA["storage_cluster_name"]
        cluster_data_yaml = tempfile.NamedTemporaryFile(
            mode="w+", prefix="external_cluster_storage", delete=False
        )
        templating.dump_data_to_temp_yaml(cluster_data, cluster_data_yaml.name)
        run_cmd(f"oc create -f {cluster_data_yaml.name}", timeout=2400)
        self.external_post_deploy_validation()
        setup_ceph_toolbox()

    def set_rook_log_level(self):
        rook_log_level = config.DEPLOYMENT.get("rook_log_level")
        if rook_log_level:
            set_configmap_log_level_rook_ceph_operator(rook_log_level)

    def external_post_deploy_validation(self):
        """
        This function validates successful deployment of OCS
        in external mode, some of the steps overlaps with
        converged mode

        """
        cephcluster = CephClusterExternal()
        cephcluster.cluster_health_check(timeout=300)

    def deploy_ocs(self):
        """
        Handle OCS deployment, since OCS deployment steps are common to any
        platform, implementing OCS deployment here in base class.
        """
        set_registry_to_managed_state()
        image = None
        ceph_cluster = ocp.OCP(kind="CephCluster", namespace=self.namespace)
        try:
            ceph_cluster.get().get("items")[0]
            logger.warning("OCS cluster already exists")
            return
        except (IndexError, CommandFailed):
            logger.info("Running OCS basic installation")

        # disconnected installation?
        load_cluster_info()
        if config.DEPLOYMENT.get("disconnected") and not config.DEPLOYMENT.get(
            "disconnected_env_skip_image_mirroring"
        ):
            image = prepare_disconnected_ocs_deployment()

        if config.DEPLOYMENT["external_mode"]:
            self.deploy_with_external_mode()
        else:
            self.deploy_ocs_via_operator(image)
            if config.ENV_DATA["mcg_only_deployment"]:
                mcg_only_post_deployment_checks()
                return

            pod = ocp.OCP(kind=constants.POD, namespace=self.namespace)
            cfs = ocp.OCP(kind=constants.CEPHFILESYSTEM, namespace=self.namespace)
            # Check for Ceph pods
            mon_pod_timeout = 900
            assert pod.wait_for_resource(
                condition="Running",
                selector="app=rook-ceph-mon",
                resource_count=3,
                timeout=mon_pod_timeout,
            )
            assert pod.wait_for_resource(
                condition="Running", selector="app=rook-ceph-mgr", timeout=600
            )
            assert pod.wait_for_resource(
                condition="Running",
                selector="app=rook-ceph-osd",
                resource_count=3,
                timeout=600,
            )

            # validate ceph mon/osd volumes are backed by pvc
            validate_cluster_on_pvc()

            # validate PDB creation of MON, MDS, OSD pods
            validate_pdb_creation()

            # check for odf-console
            ocs_version = version.get_semantic_ocs_version_from_config()
            if ocs_version >= version.VERSION_4_9:
                assert pod.wait_for_resource(
                    condition="Running", selector="app=odf-console", timeout=600
                )

            # Creating toolbox pod
            setup_ceph_toolbox()

            assert pod.wait_for_resource(
                condition=constants.STATUS_RUNNING,
                selector="app=rook-ceph-tools",
                resource_count=1,
                timeout=600,
            )

            if not config.COMPONENTS["disable_cephfs"]:
                # Check for CephFilesystem creation in ocp
                cfs_data = cfs.get()
                cfs_name = cfs_data["items"][0]["metadata"]["name"]

                if helpers.validate_cephfilesystem(cfs_name):
                    logger.info("MDS deployment is successful!")
                    defaults.CEPHFILESYSTEM_NAME = cfs_name
                else:
                    logger.error("MDS deployment Failed! Please check logs!")

        # Change monitoring backend to OCS
        if config.ENV_DATA.get("monitoring_enabled") and config.ENV_DATA.get(
            "persistent-monitoring"
        ):
            setup_persistent_monitoring()
        elif config.ENV_DATA.get("monitoring_enabled") and config.ENV_DATA.get(
            "telemeter_server_url"
        ):
            # Create configmap cluster-monitoring-config to reconfigure
            # telemeter server url when 'persistent-monitoring' is False
            create_configmap_cluster_monitoring_pod(
                telemeter_server_url=config.ENV_DATA["telemeter_server_url"]
            )

        if not config.COMPONENTS["disable_cephfs"]:
            # Change registry backend to OCS CEPHFS RWX PVC
            registry.change_registry_backend_to_ocs()

        # Enable console plugin
        enable_console_plugin()

        # Verify health of ceph cluster
        logger.info("Done creating rook resources, waiting for HEALTH_OK")
        try:
            ceph_health_check(namespace=self.namespace, tries=30, delay=10)
        except CephHealthException as ex:
            err = str(ex)
            logger.warning(f"Ceph health check failed with {err}")
            if "clock skew detected" in err:
                logger.info(
                    f"Changing NTP on compute nodes to" f" {constants.RH_NTP_CLOCK}"
                )
                if self.platform == constants.VSPHERE_PLATFORM:
                    update_ntp_compute_nodes()
                assert ceph_health_check(namespace=self.namespace, tries=60, delay=10)

        # patch gp2/thin storage class as 'non-default'
        self.patch_default_sc_to_non_default()

    def deploy_lvmo(self):
        """
        deploy lvmo for platform specific (for now only vsphere)
        """
        if not config.DEPLOYMENT["install_lvmo"]:
            logger.warning("LVMO deployment will be skipped")
            return

        logger.info(f"Installing lvmo version {config.ENV_DATA['ocs_version']}")
        lvmo_version = config.ENV_DATA["ocs_version"]
        lvmo_version_without_period = lvmo_version.replace(".", "")
        label_version = constants.LVMO_POD_LABEL
        create_catalog_source()
        # this is a workaround for 2103818
        lvm_full_version = get_lvm_full_version()
        major, minor = lvm_full_version.split("-")
        if int(minor) > 105 and major == "4.11.0":
            lvmo_version_without_period = "411"
        elif int(minor) < 105 and major == "4.11.0":
            lvmo_version_without_period = "411-old"

        file_version = lvmo_version_without_period
        if "old" in file_version:
            file_version = file_version.split("-")[0]

        cluster_config_file = os.path.join(
            constants.TEMPLATE_DEPLOYMENT_DIR_LVMO,
            f"lvm-cluster-{file_version}.yaml",
        )
        # this is a workaround for 2101343
        if 110 > int(minor) > 98 and major == "4.11.0":
            rolebinding_config_file = os.path.join(
                constants.TEMPLATE_DEPLOYMENT_DIR_LVMO, "role_rolebinding.yaml"
            )
            run_cmd(f"oc create -f {rolebinding_config_file} -n default")
        # end of workaround
        bundle_config_file = os.path.join(
            constants.TEMPLATE_DEPLOYMENT_DIR_LVMO, "lvm-bundle.yaml"
        )
        run_cmd(f"oc create -f {bundle_config_file} -n {self.namespace}")
        pod = ocp.OCP(kind=constants.POD, namespace=self.namespace)
        assert pod.wait_for_resource(
            condition="Running",
            selector=label_version[lvmo_version_without_period][
                "controller_manager_label"
            ],
            resource_count=1,
            timeout=300,
        )
        run_cmd(f"oc create -f {cluster_config_file} -n {self.namespace}")
        assert pod.wait_for_resource(
            condition="Running",
            selector=label_version[lvmo_version_without_period][
                "topolvm-controller_label"
            ],
            resource_count=1,
            timeout=300,
        )
        assert pod.wait_for_resource(
            condition="Running",
            selector=label_version[lvmo_version_without_period]["topolvm-node_label"],
            resource_count=1,
            timeout=300,
        )
        assert pod.wait_for_resource(
            condition="Running",
            selector=label_version[lvmo_version_without_period]["vg-manager_label"],
            resource_count=1,
            timeout=300,
        )
        catalgesource = run_cmd(
            "oc -n openshift-marketplace get  "
            "catalogsources.operators.coreos.com redhat-operators -o json"
        )
        json_cts = json.loads(catalgesource)
        logger.info(
            f"LVMO installed successfully from image {json_cts['spec']['image']}"
        )

    def destroy_cluster(self, log_level="DEBUG"):
        """
        Base destroy cluster method, for more platform specific stuff please
        overload this method in child class.

        Args:
            log_level (str): log level for installer (default: DEBUG)
        """
        if self.platform == constants.IBM_POWER_PLATFORM:
            if not config.ENV_DATA["skip_ocs_deployment"]:
                self.destroy_ocs()

            if not config.ENV_DATA["skip_ocp_deployment"]:
                logger.info("Destroy of OCP not implemented yet.")
        else:
            self.ocp_deployment = self.OCPDeployment()
            try:
                uninstall_ocs()
                # TODO - add ocs uninstall validation function call
                logger.info("OCS uninstalled successfully")
            except Exception as ex:
                logger.error(f"Failed to uninstall OCS. Exception is: {ex}")
                logger.info("resuming teardown")
            self.ocp_deployment.destroy(log_level)

    def add_node(self):
        """
        Implement platform-specific add_node in child class
        """
        raise NotImplementedError("add node functionality not implemented")

    def patch_default_sc_to_non_default(self):
        """
        Patch storage class which comes as default with installation to non-default
        """
        if not self.DEFAULT_STORAGECLASS:
            logger.info(
                "Default StorageClass is not set for this class: "
                f"{self.__class__.__name__}"
            )
            return
        logger.info(f"Patch {self.DEFAULT_STORAGECLASS} storageclass as non-default")
        patch = ' \'{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"false"}}}\' '
        run_cmd(
            f"oc patch storageclass {self.DEFAULT_STORAGECLASS} "
            f"-p {patch} "
            f"--request-timeout=120s"
        )

    def deploy_acm_hub(self):
        """
        Handle ACM HUB deployment
        """
        if config.ENV_DATA.get("acm_hub_unreleased"):
            self.deploy_acm_hub_unreleased()
        else:
            self.deploy_acm_hub_released()

    def deploy_acm_hub_unreleased(self):
        """
        Handle ACM HUB unreleased image deployment
        """
        logger.info("Cloning open-cluster-management deploy repository")
        acm_hub_deploy_dir = os.path.join(
            constants.EXTERNAL_DIR, "acm_hub_unreleased_deploy"
        )
        clone_repo(constants.ACM_HUB_UNRELEASED_DEPLOY_REPO, acm_hub_deploy_dir)

        logger.info("Retrieving quay token")
        docker_config = load_auth_config().get("quay", {}).get("cli_password", {})
        pw = base64.b64decode(docker_config)
        pw = pw.decode().replace("quay.io", "quay.io:443").encode()
        quay_token = base64.b64encode(pw).decode()

        kubeconfig_location = os.path.join(self.cluster_path, "auth", "kubeconfig")

        logger.info("Setting env vars")
        env_vars = {
            "QUAY_TOKEN": quay_token,
            "COMPOSITE_BUNDLE": "true",
            "CUSTOM_REGISTRY_REPO": "quay.io:443/acm-d",
            "DOWNSTREAM": "true",
            "DEBUG": "true",
            "KUBECONFIG": kubeconfig_location,
        }
        for key, value in env_vars.items():
            if value:
                os.environ[key] = value

        logger.info("Writing pull-secret")
        _templating = templating.Templating(
            os.path.join(constants.TEMPLATE_DIR, "acm-deployment")
        )
        template_data = {"docker_config": docker_config}
        data = _templating.render_template(
            constants.ACM_HUB_UNRELEASED_PULL_SECRET_TEMPLATE,
            template_data,
        )
        pull_secret_path = os.path.join(
            acm_hub_deploy_dir, "prereqs", "pull-secret.yaml"
        )
        with open(pull_secret_path, "w") as f:
            f.write(data)

        logger.info("Creating ImageContentSourcePolicy")
        run_cmd(f"oc create -f {constants.ACM_HUB_UNRELEASED_ICSP_YAML}")

        logger.info("Writing tag data to snapshot.ver")
        image_tag = config.ENV_DATA.get(
            "acm_unreleased_image", config.ENV_DATA.get("default_acm_unreleased_image")
        )
        with open(os.path.join(acm_hub_deploy_dir, "snapshot.ver"), "w") as f:
            f.write(image_tag)

        logger.info("Running open-cluster-management deploy")
        cmd = ["./start.sh", "--silent"]
        logger.info("Running cmd: %s", " ".join(cmd))
        proc = Popen(
            cmd,
            cwd=acm_hub_deploy_dir,
            stdout=PIPE,
            stderr=PIPE,
            encoding="utf-8",
        )
        stdout, stderr = proc.communicate()
        logger.info(stdout)
        if proc.returncode:
            logger.error(stderr)
            raise CommandFailed("open-cluster-management deploy script error")

        validate_acm_hub_install()

    def deploy_acm_hub_released(self):
        """
        Handle ACM HUB released image deployment
        """
        channel = config.ENV_DATA.get("acm_hub_channel")
        logger.info("Creating ACM HUB namespace")
        acm_hub_namespace_yaml_data = templating.load_yaml(constants.NAMESPACE_TEMPLATE)
        acm_hub_namespace_yaml_data["metadata"]["name"] = constants.ACM_HUB_NAMESPACE
        acm_hub_namespace_manifest = tempfile.NamedTemporaryFile(
            mode="w+", prefix="acm_hub_namespace_manifest", delete=False
        )
        templating.dump_data_to_temp_yaml(
            acm_hub_namespace_yaml_data, acm_hub_namespace_manifest.name
        )
        run_cmd(f"oc create -f {acm_hub_namespace_manifest.name}")

        logger.info("Creating OperationGroup for ACM deployment")
        package_manifest = PackageManifest(
            resource_name=constants.ACM_HUB_OPERATOR_NAME,
        )

        run_cmd(
            f"oc create -f {constants.ACM_HUB_OPERATORGROUP_YAML} -n {constants.ACM_HUB_NAMESPACE}"
        )

        logger.info("Creating ACM HUB Subscription")
        acm_hub_subscription_yaml_data = templating.load_yaml(
            constants.ACM_HUB_SUBSCRIPTION_YAML
        )
        acm_hub_subscription_yaml_data["spec"]["channel"] = channel
        acm_hub_subscription_yaml_data["spec"][
            "startingCSV"
        ] = package_manifest.get_current_csv(
            channel=channel, csv_pattern=constants.ACM_HUB_OPERATOR_NAME
        )

        acm_hub_subscription_manifest = tempfile.NamedTemporaryFile(
            mode="w+", prefix="acm_hub_subscription_manifest", delete=False
        )
        templating.dump_data_to_temp_yaml(
            acm_hub_subscription_yaml_data, acm_hub_subscription_manifest.name
        )
        run_cmd(f"oc create -f {acm_hub_subscription_manifest.name}")
        logger.info("Sleeping for 90 seconds after subscribing to ACM")
        time.sleep(90)
        csv_name = package_manifest.get_current_csv(channel=channel)
        csv = CSV(resource_name=csv_name, namespace=constants.ACM_HUB_NAMESPACE)
        csv.wait_for_phase("Succeeded", timeout=720)
        logger.info("ACM HUB Operator Deployment Succeeded")
        logger.info("Creating MultiCluster Hub")
        run_cmd(
            f"oc create -f {constants.ACM_HUB_MULTICLUSTERHUB_YAML} -n {constants.ACM_HUB_NAMESPACE}"
        )
        validate_acm_hub_install()


def validate_acm_hub_install():
    """
    Verify the ACM MultiClusterHub installation was successful.
    """
    logger.info("Verify ACM MultiClusterHub Installation")
    acm_mch = ocp.OCP(
        kind=constants.ACM_MULTICLUSTER_HUB,
        namespace=constants.ACM_HUB_NAMESPACE,
    )
    acm_mch.wait_for_resource(
        condition=constants.STATUS_RUNNING,
        resource_name=constants.ACM_MULTICLUSTER_RESOURCE,
        column="STATUS",
        timeout=720,
        sleep=5,
    )
    logger.info("MultiClusterHub Deployment Succeeded")


def create_ocs_secret(namespace):
    """
    Function for creation of pull secret for OCS. (Mostly for ibmcloud purpose)

    Args:
        namespace (str): namespace where to create the secret

    """
    secret_data = templating.load_yaml(constants.OCS_SECRET_YAML)
    docker_config_json = config.DEPLOYMENT["ocs_secret_dockerconfigjson"]
    secret_data["data"][".dockerconfigjson"] = docker_config_json
    secret_manifest = tempfile.NamedTemporaryFile(
        mode="w+", prefix="ocs_secret", delete=False
    )
    templating.dump_data_to_temp_yaml(secret_data, secret_manifest.name)
    exec_cmd(f"oc apply -f {secret_manifest.name} -n {namespace}", timeout=2400)


def create_catalog_source(image=None, ignore_upgrade=False):
    """
    This prepare catalog source manifest for deploy OCS operator from
    quay registry.

    Args:
        image (str): Image of ocs registry.
        ignore_upgrade (bool): Ignore upgrade parameter.

    """
    # Because custom catalog source will be called: redhat-operators, we need to disable
    # default sources. This should not be an issue as OCS internal registry images
    # are now based on OCP registry image
    disable_specific_source(constants.OPERATOR_CATALOG_SOURCE_NAME)
    logger.info("Adding CatalogSource")
    if not image:
        image = config.DEPLOYMENT.get("ocs_registry_image", "")
    if config.DEPLOYMENT.get("stage_rh_osbs"):
        image = config.DEPLOYMENT.get("stage_index_image", constants.OSBS_BOUNDLE_IMAGE)
        ocp_version = version.get_semantic_ocp_version_from_config()
        osbs_image_tag = config.DEPLOYMENT.get(
            "stage_index_image_tag", f"v{ocp_version}"
        )
        image += f":{osbs_image_tag}"
        run_cmd(
            "oc patch image.config.openshift.io/cluster --type merge -p '"
            '{"spec": {"registrySources": {"insecureRegistries": '
            '["registry-proxy.engineering.redhat.com", "registry.stage.redhat.io"]'
            "}}}'"
        )
        run_cmd(f"oc apply -f {constants.STAGE_IMAGE_CONTENT_SOURCE_POLICY_YAML}")
        wait_for_machineconfigpool_status("all", timeout=1800)
    if not ignore_upgrade:
        upgrade = config.UPGRADE.get("upgrade", False)
    else:
        upgrade = False
    image_and_tag = image.rsplit(":", 1)
    image = image_and_tag[0]
    image_tag = image_and_tag[1] if len(image_and_tag) == 2 else None
    if not image_tag and config.REPORTING.get("us_ds") == "DS":
        image_tag = get_latest_ds_olm_tag(
            upgrade, latest_tag=config.DEPLOYMENT.get("default_latest_tag", "latest")
        )

    catalog_source_data = templating.load_yaml(constants.CATALOG_SOURCE_YAML)
    if config.ENV_DATA["platform"] == constants.IBMCLOUD_PLATFORM:
        create_ocs_secret(constants.MARKETPLACE_NAMESPACE)
        catalog_source_data["spec"]["secrets"] = [constants.OCS_SECRET]
    cs_name = constants.OPERATOR_CATALOG_SOURCE_NAME
    change_cs_condition = (
        (image or image_tag)
        and catalog_source_data["kind"] == "CatalogSource"
        and catalog_source_data["metadata"]["name"] == cs_name
    )
    if change_cs_condition:
        default_image = config.DEPLOYMENT["default_ocs_registry_image"]
        image = image if image else default_image.rsplit(":", 1)[0]
        catalog_source_data["spec"][
            "image"
        ] = f"{image}:{image_tag if image_tag else 'latest'}"
    catalog_source_manifest = tempfile.NamedTemporaryFile(
        mode="w+", prefix="catalog_source_manifest", delete=False
    )
    templating.dump_data_to_temp_yaml(catalog_source_data, catalog_source_manifest.name)
    run_cmd(f"oc apply -f {catalog_source_manifest.name}", timeout=2400)
    catalog_source = CatalogSource(
        resource_name=constants.OPERATOR_CATALOG_SOURCE_NAME,
        namespace=constants.MARKETPLACE_NAMESPACE,
    )
    # Wait for catalog source is ready
    catalog_source.wait_for_state("READY")


@retry(CommandFailed, tries=8, delay=3)
def setup_persistent_monitoring():
    """
    Change monitoring backend to OCS
    """
    sc = helpers.default_storage_class(interface_type=constants.CEPHBLOCKPOOL)

    # Get the list of monitoring pods
    pods_list = get_all_pods(
        namespace=defaults.OCS_MONITORING_NAMESPACE,
        selector=["prometheus", "alertmanager"],
    )

    # Create configmap cluster-monitoring-config and reconfigure
    # storage class and telemeter server (if the url is specified in a
    # config file)
    create_configmap_cluster_monitoring_pod(
        sc_name=sc.name,
        telemeter_server_url=config.ENV_DATA.get("telemeter_server_url"),
    )

    # Take some time to respin the pod
    waiting_time = 45
    logger.info(f"Waiting {waiting_time} seconds...")
    time.sleep(waiting_time)

    # Validate the pods are respinned and in running state
    retry((CommandFailed, ResourceWrongStatusException), tries=3, delay=15)(
        validate_pods_are_respinned_and_running_state
    )(pods_list)

    # Validate the pvc is created on monitoring pods
    validate_pvc_created_and_bound_on_monitoring_pods()

    # Validate the pvc are mounted on pods
    retry((CommandFailed, AssertionError), tries=3, delay=15)(
        validate_pvc_are_mounted_on_monitoring_pods
    )(pods_list)


class RBDDRDeployOps(object):
    """
    All RBD specific DR deployment operations

    """

    def deploy(self):
        self.configure_mirror_peer()

    def configure_mirror_peer(self):
        # Current CTX: ACM
        # Create mirror peer
        mirror_peer_data = templating.load_yaml(constants.MIRROR_PEER)
        mirror_peer_yaml = tempfile.NamedTemporaryFile(
            mode="w+", prefix="mirror_peer", delete=False
        )
        # Update all the participating clusters in mirror_peer_yaml
        non_acm_clusters = get_non_acm_cluster_config()
        primary = get_primary_cluster_config()
        non_acm_clusters.remove(primary)
        for cluster in non_acm_clusters:
            logger.info(f"{cluster.ENV_DATA['cluster_name']}")
        index = -1
        # First entry should be the primary cluster
        # in the mirror peer
        for cluster_entry in mirror_peer_data["spec"]["items"]:
            if index == -1:
                cluster_entry["clusterName"] = primary.ENV_DATA["cluster_name"]
            else:
                cluster_entry["clusterName"] = non_acm_clusters[index].ENV_DATA[
                    "cluster_name"
                ]
            index += 1
        templating.dump_data_to_temp_yaml(mirror_peer_data, mirror_peer_yaml.name)
        # Current CTX: ACM
        # Just being explicit here to make code more readable
        config.switch_acm_ctx()
        run_cmd(f"oc create -f {mirror_peer_yaml.name}")
        self.validate_mirror_peer(mirror_peer_data["metadata"]["name"])

        st_string = '{.items[?(@.metadata.ownerReferences[*].kind=="StorageCluster")].spec.mirroring.enabled}'
        query_mirroring = (
            f"oc get CephBlockPool -n {constants.OPENSHIFT_STORAGE_NAMESPACE}"
            f" -o=jsonpath='{st_string}'"
        )
        out_list = run_cmd_multicluster(
            query_mirroring, skip_index=config.get_acm_index()
        )
        index = 0
        for out in out_list:
            if not out:
                continue
            logger.info(out.stdout)
            if out.stdout.decode() != "true":
                logger.error(
                    f"On cluster {config.clusters[index].ENV_DATA['cluster_name']}"
                )
                raise ResourceWrongStatusException(
                    "CephBlockPool", expected="true", got=out
                )
            index = +1
        # Check for RBD mirroring pods
        for cluster in get_non_acm_cluster_config():
            config.switch_ctx(cluster.MULTICLUSTER["multicluster_index"])
            mirror_pod = get_pod_count(label="app=rook-ceph-rbd-mirror")
            if not mirror_pod:
                raise PodNotCreated(
                    f"RBD mirror pod not found on cluster: "
                    f"{cluster.ENV_DATA['cluster_name']}"
                )
            self.validate_csi_sidecar()

        # Reset CTX back to ACM
        config.switch_acm_ctx()

    def validate_csi_sidecar(self):
        """
        validate sidecar containers for rbd mirroring on each of the
        ODF cluster

        """
        # Number of containers should be 8/8 from 2 pods now which makes total 16 containers
        rbd_pods = (
            f"oc get pods -n {constants.OPENSHIFT_STORAGE_NAMESPACE} "
            f"-l app=csi-rbdplugin-provisioner -o jsonpath={{.items[*].spec.containers[*].name}}"
        )
        timeout = 10
        while timeout:
            out = run_cmd(rbd_pods)
            logger.info(out)
            logger.info(len(out.split(" ")))
            if constants.RBD_SIDECAR_COUNT != len(out.split(" ")):
                time.sleep(2)
            else:
                break
            timeout -= 1
        if not timeout:
            raise RBDSideCarContainerException("RBD Sidecar container count mismatch")

    def validate_mirror_peer(self, resource_name):
        """
        Validate mirror peer,
        Begins with CTX: ACM

        1. Check initial phase of 'ExchangingSecret'
        2. Check token-exchange-agent pod in 'Running' phase

        Raises:
            ResourceWrongStatusException: If pod is not in expected state

        """
        # Check mirror peer status only on HUB
        mirror_peer = ocp.OCP(
            kind="MirrorPeer",
            namespace=constants.DR_DEFAULT_NAMESPACE,
            resource_name=resource_name,
        )
        mirror_peer._has_phase = True
        mirror_peer.get()
        try:
            mirror_peer.wait_for_phase(phase="ExchangedSecret", timeout=1200)
            logger.info("Mirror peer is in expected phase 'ExchangedSecret'")
        except ResourceWrongStatusException:
            logger.exception("Mirror peer couldn't attain expected phase")
            raise

        # Check for token-exchange-agent pod and its status has to be running
        # on all participating clusters except HUB
        # We will switch config ctx to Participating clusters
        for cluster in config.clusters:
            if cluster.MULTICLUSTER["multicluster_index"] == config.get_acm_index():
                continue
            else:
                config.switch_ctx(cluster.MULTICLUSTER["multicluster_index"])
                token_xchange_agent = get_pods_having_label(
                    constants.TOKEN_EXCHANGE_AGENT_LABEL,
                    constants.OPENSHIFT_STORAGE_NAMESPACE,
                )
                pod_status = token_xchange_agent[0]["status"]["phase"]
                pod_name = token_xchange_agent[0]["metadata"]["name"]
                if pod_status != "Running":
                    logger.error(f"On cluster {cluster.ENV_DATA['cluster_name']}")
                    ResourceWrongStatusException(
                        pod_name, expected="Running", got=pod_status
                    )
        # Switching back CTX to ACM
        config.switch_acm_ctx()


class MultiClusterDROperatorsDeploy(object):
    """
    Implement Multicluster DR operators deploy part here, mainly
    1. ODF Multicluster Orchestrator operator
    2. Metadata object stores (s3 OR MCG)
    3. ODF Hub operator
    4. ODF Cluster operator

    """

    def __init__(self, dr_conf):
        # DR use case could be RBD or CephFS or Both
        self.rbd = dr_conf.get("rbd_dr_scenario", False)
        # CephFS For future usecase
        self.cephfs = dr_conf.get("cephfs_dr_scenario", False)
        self.meta_map = {
            "awss3": self.s3_meta_obj_store,
            "mcg": self.mcg_meta_obj_store,
        }
        # Default to s3 for metadata store
        self.meta_obj_store = dr_conf.get("dr_metadata_store", "awss3")
        self.meta_obj = self.meta_map[self.meta_obj_store]()
        self.channel = config.DEPLOYMENT.get("ocs_csv_channel")

    def deploy(self):
        """
        deploy ODF multicluster orchestrator operator

        """
        # current CTX: ACM
        config.switch_acm_ctx()
        # Create openshift-dr-system namespace
        run_cmd_multicluster(
            f"oc create -f {constants.OPENSHIFT_DR_SYSTEM_NAMESPACE_YAML} ",
        )
        self.deploy_dr_multicluster_orchestrator()
        # create this only on ACM
        run_cmd(
            f"oc create -f {constants.OPENSHIFT_DR_SYSTEM_OPERATORGROUP}",
        )
        self.deploy_dr_hub_operator()

        # RBD specific dr deployment
        if self.rbd:
            rbddops = RBDDRDeployOps()
            rbddops.deploy()
            self.meta_obj.deploy_and_configure()

        self.deploy_dr_policy()
        self.meta_obj.conf.update({"dr_policy_name": self.dr_policy_name})
        self.update_ramen_config_misc()

    def deploy_dr_multicluster_orchestrator(self):
        """
        Deploy multicluster orchestrator
        """
        odf_multicluster_orchestrator_data = templating.load_yaml(
            constants.ODF_MULTICLUSTER_ORCHESTRATOR
        )
        package_manifest = packagemanifest.PackageManifest(
            resource_name=constants.ACM_ODF_MULTICLUSTER_ORCHESTRATOR_RESOURCE
        )
        current_csv = package_manifest.get_current_csv(
            channel=self.channel,
            csv_pattern=constants.ACM_ODF_MULTICLUSTER_ORCHESTRATOR_RESOURCE,
        )
        logger.info(f"CurrentCSV={current_csv}")
        odf_multicluster_orchestrator_data["spec"]["channel"] = self.channel
        odf_multicluster_orchestrator_data["spec"]["startingCSV"] = current_csv
        odf_multicluster_orchestrator = tempfile.NamedTemporaryFile(
            mode="w+", prefix="odf_multicluster_orchestrator", delete=False
        )
        templating.dump_data_to_temp_yaml(
            odf_multicluster_orchestrator_data, odf_multicluster_orchestrator.name
        )
        run_cmd(f"oc create -f {odf_multicluster_orchestrator.name}")
        orchestrator_controller = ocp.OCP(
            kind="Deployment",
            resource_name=constants.ODF_MULTICLUSTER_ORCHESTRATOR_CONTROLLER_MANAGER,
            namespace=constants.OPENSHIFT_OPERATORS,
        )
        orchestrator_controller.wait_for_resource(
            condition="1", column="AVAILABLE", resource_count=1, timeout=600
        )

    def update_ramen_config_misc(self):
        config_map_data = self.meta_obj.get_ramen_resource()
        self.update_config_map_commit(config_map_data.data)

    def update_config_map_commit(self, config_map_data, prefix=None):
        """
        merge the config and update the resource

        Args:
            config_map_data (dict): base dictionary which will be later converted to yaml content
            prefix (str): Used to identify temp yaml

        """
        logger.debug(
            "Converting Ramen section (which is string) to dict and updating "
            "config_map_data with the same dict"
        )
        ramen_section = {
            f"{constants.DR_RAMEN_CONFIG_MANAGER_KEY}": yaml.safe_load(
                config_map_data["data"].pop(f"{constants.DR_RAMEN_CONFIG_MANAGER_KEY}")
            )
        }
        ramen_section[constants.DR_RAMEN_CONFIG_MANAGER_KEY][
            "drClusterOperator"
        ].update({"deploymentAutomationEnabled": True})
        logger.debug("Merge back the ramen_section with config_map_data")
        config_map_data["data"].update(ramen_section)
        for key in ["annotations", "creationTimestamp", "resourceVersion", "uid"]:
            if config_map_data["metadata"].get(key):
                config_map_data["metadata"].pop(key)

        dr_ramen_configmap_yaml = tempfile.NamedTemporaryFile(
            mode="w+", prefix=prefix, delete=False
        )
        yaml_serialized = yaml.dump(config_map_data)
        logger.debug(
            "Update yaml stream with a '|' for literal interpretation"
            " which comes exactly right after the key 'ramen_manager_config.yaml'"
        )
        yaml_serialized = yaml_serialized.replace(
            f"{constants.DR_RAMEN_CONFIG_MANAGER_KEY}:",
            f"{constants.DR_RAMEN_CONFIG_MANAGER_KEY}: |",
        )
        logger.info(f"after serialize {yaml_serialized}")
        dr_ramen_configmap_yaml.write(yaml_serialized)
        dr_ramen_configmap_yaml.flush()
        run_cmd(f"oc apply -f {dr_ramen_configmap_yaml.name}")

    def deploy_dr_hub_operator(self):
        # Create ODF HUB operator only on ACM HUB
        dr_hub_operator_data = templating.load_yaml(constants.OPENSHIFT_DR_HUB_OPERATOR)
        dr_hub_operator_yaml = tempfile.NamedTemporaryFile(
            mode="w+", prefix="dr_hub_operator_", delete=False
        )
        package_manifest = PackageManifest(
            resource_name=constants.ACM_ODR_HUB_OPERATOR_RESOURCE
        )
        current_csv = package_manifest.get_current_csv(
            channel=self.channel, csv_pattern=constants.ACM_ODR_HUB_OPERATOR_RESOURCE
        )
        dr_hub_operator_data["spec"]["channel"] = self.channel
        dr_hub_operator_data["spec"]["startingCSV"] = current_csv
        templating.dump_data_to_temp_yaml(
            dr_hub_operator_data, dr_hub_operator_yaml.name
        )
        run_cmd(f"oc create -f {dr_hub_operator_yaml.name}")
        logger.info("Sleeping for 90 seconds after subscribing ")
        time.sleep(90)
        dr_hub_csv = CSV(
            resource_name=current_csv,
            namespace=constants.OPENSHIFT_DR_SYSTEM_NAMESPACE,
        )
        dr_hub_csv.wait_for_phase("Succeeded")

    def deploy_dr_policy(self):
        # Create DR policy on ACM hub cluster
        dr_policy_hub_data = templating.load_yaml(constants.DR_POLICY_ACM_HUB)
        s3profiles = self.meta_obj.get_s3_profiles()
        # Update DR cluster name and s3profile name
        for (cluster, name_entry) in zip(
            get_non_acm_cluster_config(), dr_policy_hub_data["spec"]["drClusterSet"]
        ):
            name_entry["name"] = cluster.ENV_DATA["cluster_name"]
            for profile_name in s3profiles:
                if cluster.ENV_DATA["cluster_name"] in profile_name:
                    name_entry["s3ProfileName"] = profile_name
            if not name_entry["s3ProfileName"]:
                raise RDRDeploymentException(
                    f"Not able to find s3profile for cluster {cluster.ENV_DATA['cluster_name']},"
                    f"Regional DR deployment will not succeed"
                )

        dr_policy_hub_yaml = tempfile.NamedTemporaryFile(
            mode="w+", prefix="dr_policy_hub_", delete=False
        )
        templating.dump_data_to_temp_yaml(dr_policy_hub_data, dr_policy_hub_yaml.name)
        self.dr_policy_name = dr_policy_hub_data["metadata"]["name"]
        run_cmd(f"oc create -f {dr_policy_hub_yaml.name}")
        # Check the status of DRPolicy and wait for 'Reason' field to be set to 'Succeeded'
        dr_policy_resource = ocp.OCP(
            kind="DRPolicy",
            resource_name=self.dr_policy_name,
            namespace=constants.OPENSHIFT_DR_SYSTEM_NAMESPACE,
        )
        dr_policy_resource.get()
        sample = TimeoutSampler(
            timeout=600,
            sleep=3,
            func=self.meta_obj._get_status,
            resource_data=dr_policy_resource,
        )
        if not sample.wait_for_func_status(True):
            raise TimeoutExpiredError("DR Policy failed to reach Succeeded state")

    class s3_meta_obj_store:
        """
        Internal class to handle aws s3 metadata obj store

        """

        def __init__(self, conf=None):
            self.dr_regions = self.get_participating_regions()
            self.conf = conf if conf else dict()

        def deploy_and_configure(self):
            self.s3_configure()

        def s3_configure(self):
            # Configure s3secret on both primary and secondary clusters
            secret_yaml_files = []
            secret_names = self.get_s3_secret_names()
            for secret in secret_names:
                secret_data = ocp.OCP(
                    kind="Secret",
                    resource_name=secret,
                    namespace=constants.OPENSHIFT_DR_SYSTEM_NAMESPACE,
                )
                secret_data.get()
                for key in ["creationTimestamp", "resourceVersion", "uid"]:
                    secret_data.data["metadata"].pop(key)
                secret_temp_file = tempfile.NamedTemporaryFile(
                    mode="w+", prefix=secret, delete=False
                )
                templating.dump_data_to_temp_yaml(
                    secret_data.data, secret_temp_file.name
                )
                secret_yaml_files.append(secret_temp_file.name)

            # Create s3 secret on all clusters except ACM
            for secret_yaml in secret_yaml_files:
                cmd = f"oc create -f {secret_yaml}"
                run_cmd_multicluster(cmd, skip_index=config.get_acm_index())

        def get_participating_regions(self):
            """
            Get all the participating regions in the DR scenario

            Returns:
                list of str: List of participating regions

            """
            # For first cut just returning east and west
            return ["east", "west"]

        def get_s3_secret_names(self):
            """
            Get secret resource names for s3

            """
            s3_secrets = []
            dr_ramen_hub_configmap_data = self.get_ramen_resource()
            ramen_config = yaml.safe_load(
                dr_ramen_hub_configmap_data.data["data"]["ramen_manager_config.yaml"]
            )
            for s3profile in ramen_config["s3StoreProfiles"]:
                s3_secrets.append(s3profile["s3SecretRef"]["name"])
            return s3_secrets

        def get_s3_profiles(self):
            """
            Get names of s3 profiles from hub configmap resource

            """
            s3_profiles = []
            dr_ramen_hub_configmap_data = self.get_ramen_resource()
            ramen_config = yaml.safe_load(
                dr_ramen_hub_configmap_data.data["data"]["ramen_manager_config.yaml"]
            )
            for s3profile in ramen_config["s3StoreProfiles"]:
                s3_profiles.append(s3profile["s3ProfileName"])

            return s3_profiles

        def get_ramen_resource(self):
            dr_ramen_hub_configmap_data = ocp.OCP(
                kind="ConfigMap",
                resource_name=constants.DR_RAMEN_HUB_OPERATOR_CONFIG,
                namespace=constants.OPENSHIFT_DR_SYSTEM_NAMESPACE,
            )
            dr_ramen_hub_configmap_data.get()
            return dr_ramen_hub_configmap_data

        def _get_status(self, resource_data):
            resource_data.reload_data()
            reason = resource_data.data.get("status").get("conditions")[0].get("reason")
            if reason == "Succeeded":
                return True
            return False

    class mcg_meta_obj_store:
        def __init__(self):
            raise NotImplementedError("MCG metadata store support not implemented")
