"""
In this pytest plugin we will keep all our pytest marks used in our tests and
all related hooks/plugins to markers.
"""
import os

import pytest
from funcy import compose

from ocs_ci.framework import config
from ocs_ci.ocs.constants import (
    ORDER_BEFORE_OCS_UPGRADE,
    ORDER_BEFORE_OCP_UPGRADE,
    ORDER_BEFORE_UPGRADE,
    ORDER_OCP_UPGRADE,
    ORDER_OCS_UPGRADE,
    ORDER_AFTER_OCP_UPGRADE,
    ORDER_AFTER_OCS_UPGRADE,
    ORDER_AFTER_UPGRADE,
    CLOUD_PLATFORMS,
    ON_PREM_PLATFORMS,
    IBM_POWER_PLATFORM,
    IBMCLOUD_PLATFORM,
    ROSA_PLATFORM,
    OPENSHIFT_DEDICATED_PLATFORM,
    MANAGED_SERVICE_PLATFORMS,
    HPCS_KMS_PROVIDER,
)
from ocs_ci.utility import version
from ocs_ci.utility.aws import update_config_from_s3
from ocs_ci.utility.utils import load_auth_config

# tier marks

tier1 = pytest.mark.tier1(value=1)
tier2 = pytest.mark.tier2(value=2)
tier3 = pytest.mark.tier3(value=3)
tier4 = pytest.mark.tier4(value=4)
tier4a = compose(tier4, pytest.mark.tier4a)
tier4b = compose(tier4, pytest.mark.tier4b)
tier4c = compose(tier4, pytest.mark.tier4c)
tier_after_upgrade = pytest.mark.tier_after_upgrade(value=5)


# build acceptance
acceptance = pytest.mark.acceptance

# team marks

e2e = pytest.mark.e2e
ecosystem = pytest.mark.ecosystem
manage = pytest.mark.manage
libtest = pytest.mark.libtest

team_marks = [manage, ecosystem, e2e]

# components  and other markers
ocp = pytest.mark.ocp
rook = pytest.mark.rook
ui = pytest.mark.ui
csi = pytest.mark.csi
monitoring = pytest.mark.monitoring
workloads = pytest.mark.workloads
flowtests = pytest.mark.flowtests
system_test = pytest.mark.system_test
performance = pytest.mark.performance
performance_extended = pytest.mark.performance_extended
scale = pytest.mark.scale
scale_long_run = pytest.mark.scale_long_run
scale_changed_layout = pytest.mark.scale_changed_layout
deployment = pytest.mark.deployment
polarion_id = pytest.mark.polarion_id
bugzilla = pytest.mark.bugzilla
acm_import = pytest.mark.acm_import

tier_marks = [
    tier1,
    tier2,
    tier3,
    tier4,
    tier4a,
    tier4b,
    tier4c,
    tier_after_upgrade,
    performance,
    scale,
    scale_long_run,
    scale_changed_layout,
    workloads,
]

# upgrade related markers
# Requires pytest ordering plugin installed
# Use only one of those marker on one test case!
order_pre_upgrade = pytest.mark.run(order=ORDER_BEFORE_UPGRADE)
order_pre_ocp_upgrade = pytest.mark.run(order=ORDER_BEFORE_OCP_UPGRADE)
order_pre_ocs_upgrade = pytest.mark.run(order=ORDER_BEFORE_OCS_UPGRADE)
order_ocp_upgrade = pytest.mark.run(order=ORDER_OCP_UPGRADE)
order_ocs_upgrade = pytest.mark.run(order=ORDER_OCS_UPGRADE)
order_post_upgrade = pytest.mark.run(order=ORDER_AFTER_UPGRADE)
order_post_ocp_upgrade = pytest.mark.run(order=ORDER_AFTER_OCP_UPGRADE)
order_post_ocs_upgrade = pytest.mark.run(order=ORDER_AFTER_OCS_UPGRADE)
ocp_upgrade = compose(pytest.mark.ocp_upgrade, order_ocp_upgrade)
ocs_upgrade = compose(pytest.mark.ocs_upgrade, order_ocs_upgrade)
pre_upgrade = compose(pytest.mark.pre_upgrade, order_pre_upgrade)
pre_ocp_upgrade = compose(pytest.mark.pre_ocp_upgrade, order_pre_ocp_upgrade)
pre_ocs_upgrade = compose(pytest.mark.pre_ocs_upgrade, order_pre_ocs_upgrade)
post_upgrade = compose(pytest.mark.post_upgrade, order_post_upgrade)
post_ocp_upgrade = compose(pytest.mark.post_ocp_upgrade, order_post_ocp_upgrade)
post_ocs_upgrade = compose(pytest.mark.post_ocs_upgrade, order_post_ocs_upgrade)

# mark the test class with marker below to ignore leftover check
ignore_leftovers = pytest.mark.ignore_leftovers

# Mark the test class with marker below to ignore leftover of resources having
# the app labels specified
ignore_leftover_label = pytest.mark.ignore_leftover_label

# testing marker this is just for testing purpose if you want to run some test
# under development, you can mark it with @run_this and run pytest -m run_this
run_this = pytest.mark.run_this

# Skip marks
skip_inconsistent = pytest.mark.skip(
    reason="Currently the reduction is too inconsistent leading to inconsistent test results"
)

# Skipif marks
skipif_aws_creds_are_missing = pytest.mark.skipif(
    (
        load_auth_config().get("AUTH", {}).get("AWS", {}).get("AWS_ACCESS_KEY_ID")
        is None
        and "AWS_ACCESS_KEY_ID" not in os.environ
        and update_config_from_s3() is None
    ),
    reason=(
        "AWS credentials weren't found in the local auth.yaml "
        "and couldn't be fetched from the cloud"
    ),
)

skipif_mcg_only = pytest.mark.skipif(
    config.ENV_DATA["mcg_only_deployment"],
    reason="This test cannot run on MCG-Only deployments",
)

google_api_required = pytest.mark.skipif(
    not os.path.exists(os.path.expanduser(config.RUN["google_api_secret"])),
    reason="Google API credentials don't exist",
)

aws_platform_required = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() != "aws",
    reason="Test runs ONLY on AWS deployed cluster",
)

aws_based_platform_required = pytest.mark.skipif(
    (
        config.ENV_DATA["platform"].lower() != "aws"
        and config.ENV_DATA["platform"].lower() != ROSA_PLATFORM
    ),
    reason="Test runs ONLY on AWS based deployed cluster",
)
azure_platform_required = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() != "azure",
    reason="Test runs ONLY on Azure deployed cluster",
)

gcp_platform_required = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() != "gcp",
    reason="Test runs ONLY on GCP deployed cluster",
)

cloud_platform_required = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() not in CLOUD_PLATFORMS,
    reason="Test runs ONLY on cloud based deployed cluster",
)

ibmcloud_platform_required = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() != IBMCLOUD_PLATFORM,
    reason="Test runs ONLY on IBM cloud",
)

on_prem_platform_required = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() not in ON_PREM_PLATFORMS,
    reason="Test runs ONLY on on-prem based deployed cluster",
)

rh_internal_lab_required = pytest.mark.skipif(
    (
        config.ENV_DATA["platform"].lower() == "aws"
        or config.ENV_DATA["platform"].lower() == "azure"
    ),
    reason="Tests will not run in AWS or Azure Cloud",
)

vsphere_platform_required = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() != "vsphere",
    reason="Test runs ONLY on VSPHERE deployed cluster",
)
rhv_platform_required = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() != "rhv",
    reason="Test runs ONLY on RHV deployed cluster",
)

ipi_deployment_required = pytest.mark.skipif(
    config.ENV_DATA["deployment_type"].lower() != "ipi",
    reason="Test runs ONLY on IPI deployed cluster",
)

managed_service_required = pytest.mark.skipif(
    (config.ENV_DATA["platform"].lower() not in MANAGED_SERVICE_PLATFORMS),
    reason="Test runs ONLY on OSD or ROSA cluster",
)

ms_provider_required = pytest.mark.skipif(
    not (
        config.ENV_DATA["platform"].lower() in MANAGED_SERVICE_PLATFORMS
        and config.ENV_DATA["cluster_type"].lower() == "provider"
    ),
    reason="Test runs ONLY on managed service provider cluster",
)

ms_consumer_required = pytest.mark.skipif(
    not (
        config.ENV_DATA["platform"].lower() in MANAGED_SERVICE_PLATFORMS
        and config.ENV_DATA["cluster_type"].lower() == "consumer"
    ),
    reason="Test runs ONLY on managed service consumer cluster",
)

kms_config_required = pytest.mark.skipif(
    (
        config.ENV_DATA["KMS_PROVIDER"].lower() != HPCS_KMS_PROVIDER
        and load_auth_config().get("vault", {}).get("VAULT_ADDR") is None
    )
    or (
        not (
            config.ENV_DATA["KMS_PROVIDER"].lower() == HPCS_KMS_PROVIDER
            and version.get_semantic_ocs_version_from_config() >= version.VERSION_4_10
            and load_auth_config().get("hpcs", {}).get("IBM_KP_SERVICE_INSTANCE_ID")
            is not None,
        )
    ),
    reason="KMS config not found in auth.yaml",
)

skipif_aws_i3 = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() == "aws"
    and config.DEPLOYMENT.get("local_storage") is True,
    reason="Test will not run on AWS i3",
)

skipif_bm = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() == "baremetal"
    and config.DEPLOYMENT.get("local_storage") is True,
    reason="Test will not run on Bare Metal",
)

skipif_bmpsi = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() == "baremetalpsi"
    and config.DEPLOYMENT.get("local_storage") is True,
    reason="Test will not run on Baremetal PSI",
)

skipif_managed_service = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() in MANAGED_SERVICE_PLATFORMS,
    reason="Test will not run on Managed service cluster",
)

skipif_openshift_dedicated = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() == OPENSHIFT_DEDICATED_PLATFORM,
    reason="Test will not run on Openshift dedicated cluster",
)

skipif_ms_provider = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() in MANAGED_SERVICE_PLATFORMS
    and config.ENV_DATA["cluster_type"].lower() == "provider",
    reason="Test will not run on Managed service provider cluster",
)

skipif_ms_consumer = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() in MANAGED_SERVICE_PLATFORMS
    and config.ENV_DATA["cluster_type"].lower() == "consumer",
    reason="Test will not run on Managed service consumer cluster",
)

skipif_rosa = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() == ROSA_PLATFORM,
    reason="Test will not run on ROSA cluster",
)
skipif_ibm_cloud = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() == IBMCLOUD_PLATFORM,
    reason="Test will not run on IBM cloud",
)

skipif_ibm_power = pytest.mark.skipif(
    config.ENV_DATA["platform"].lower() == IBM_POWER_PLATFORM,
    reason="Test will not run on IBM Power",
)

skipif_disconnected_cluster = pytest.mark.skipif(
    config.DEPLOYMENT.get("disconnected") is True,
    reason="Test will not run on disconnected clusters",
)

skipif_proxy_cluster = pytest.mark.skipif(
    config.DEPLOYMENT.get("proxy") is True,
    reason="Test will not run on proxy clusters",
)

skipif_external_mode = pytest.mark.skipif(
    config.DEPLOYMENT.get("external_mode") is True,
    reason="Test will not run on External Mode cluster",
)

skipif_lso = pytest.mark.skipif(
    config.DEPLOYMENT.get("local_storage") is True,
    reason="Test will not run on LSO deployed cluster",
)

skipif_no_lso = pytest.mark.skipif(
    not config.DEPLOYMENT.get("local_storage"),
    reason="Test run only on LSO deployed cluster",
)

skipif_rhel_os = pytest.mark.skipif(
    (config.ENV_DATA.get("rhel_workers", None) is True)
    or (config.ENV_DATA.get("rhel_user", None) is not None),
    reason="Test will not run on cluster with RHEL OS",
)

skipif_vsphere_ipi = pytest.mark.skipif(
    (
        config.ENV_DATA["platform"].lower() == "vsphere"
        and config.ENV_DATA["deployment_type"].lower() == "ipi"
    ),
    reason="Test will not run on vSphere IPI cluster",
)

skipif_tainted_nodes = pytest.mark.skipif(
    config.DEPLOYMENT.get("infra_nodes") is True
    or config.DEPLOYMENT.get("ocs_operator_nodes_to_taint") > 0,
    reason="Test will not run if nodes are tainted",
)

skipif_flexy_deployment = pytest.mark.skipif(
    config.ENV_DATA.get("flexy_deployment"),
    reason="This test doesn't work correctly on OCP cluster deployed via Flexy",
)

metrics_for_external_mode_required = pytest.mark.skipif(
    version.get_semantic_ocs_version_from_config() < version.VERSION_4_6
    and config.DEPLOYMENT.get("external_mode") is True,
    reason="Metrics is not enabled for external mode OCS <4.6",
)

# Filter warnings
filter_insecure_request_warning = pytest.mark.filterwarnings(
    "ignore::urllib3.exceptions.InsecureRequestWarning"
)

# collect Prometheus metrics if test fails with this mark
# specify Prometheus metric names in argument
gather_metrics_on_fail = pytest.mark.gather_metrics_on_fail

# here is the place to implement some plugins hooks which will process marks
# if some operation needs to be done for some specific marked tests.

# Marker for skipping tests based on OCP version
skipif_ocp_version = pytest.mark.skipif_ocp_version

# Marker for skipping tests based on OCS version
skipif_ocs_version = pytest.mark.skipif_ocs_version

# Marker for skipping tests based on UI
skipif_ui_not_support = pytest.mark.skipif_ui_not_support

# Marker for skipping tests if the cluster is upgraded from a particular
# OCS version
skipif_upgraded_from = pytest.mark.skipif_upgraded_from
skipif_lvm_not_installed = pytest.mark.skipif_lvm_not_installed
# Marker for skipping tests if the cluster doesn't have configured cluster-wide
# encryption with KMS properly
skipif_no_kms = pytest.mark.skipif_no_kms

skipif_ibm_flash = pytest.mark.skipif(
    config.ENV_DATA.get("ibm_flash"),
    reason="This test doesn't work correctly on IBM Flash system",
)

# Squad marks
aqua_squad = pytest.mark.aqua_squad
black_squad = pytest.mark.black_squad
blue_squad = pytest.mark.blue_squad
brown_squad = pytest.mark.brown_squad
green_squad = pytest.mark.green_squad
grey_squad = pytest.mark.grey_squad
magenta_squad = pytest.mark.magenta_squad
orange_squad = pytest.mark.orange_squad
purple_squad = pytest.mark.purple_squad
red_squad = pytest.mark.red_squad

# Marks to identify the cluster type in which the test case should run
runs_on_provider = pytest.mark.runs_on_provider
