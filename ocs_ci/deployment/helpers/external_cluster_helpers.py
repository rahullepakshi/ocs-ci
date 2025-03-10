"""
This module contains helpers functions needed for
external cluster deployment.
"""

import json
import logging
import re
import tempfile

from ocs_ci.framework import config
from ocs_ci.ocs import defaults
from ocs_ci.ocs.exceptions import (
    ExternalClusterExporterRunFailed,
    ExternalClusterRGWEndPointMissing,
    ExternalClusterRGWEndPointPortMissing,
    ExternalClusterObjectStoreUserCreationFailed,
    ExternalClusterCephfsMissing,
)
from ocs_ci.ocs.resources.packagemanifest import (
    PackageManifest,
    get_selector_for_ocs_operator,
)
from ocs_ci.ocs.resources.ocs import get_ocs_csv
from ocs_ci.utility import version
from ocs_ci.utility.connection import Connection
from ocs_ci.utility.utils import upload_file, encode, decode

logger = logging.getLogger(__name__)


class ExternalCluster(object):
    """
    Helper for External RHCS cluster
    """

    def __init__(self, host, user, password):
        """
        Initialize the variables required for external RHCS cluster

        Args:
             host (str): Host name with FQDN or IP
             user (str): User name
             password (password): Password for the Host

        """
        self.host = host
        self.user = user
        self.password = password
        self.rhcs_conn = Connection(
            host=self.host, user=self.user, password=self.password
        )

    def get_external_cluster_details(self):
        """
        Gets the external RHCS cluster details and updates to config.EXTERNAL_MODE

        Raises:
            ExternalClusterExporterRunFailed: If exporter script failed to run on external RHCS cluster

        """
        # get rgw endpoint port
        rgw_endpoint_port = self.get_rgw_endpoint_api_port()

        # get rgw endpoint
        rgw_endpoint = get_rgw_endpoint()
        rgw_endpoint_with_port = f"{rgw_endpoint}:{rgw_endpoint_port}"

        # get ceph filesystem
        ceph_fs_name = config.ENV_DATA.get("cephfs_name") or self.get_ceph_fs()

        rbd_name = config.ENV_DATA.get("rbd_name") or defaults.RBD_NAME

        params = (
            f"--rbd-data-pool-name {rbd_name} --rgw-endpoint {rgw_endpoint_with_port}"
        )

        if config.ENV_DATA["restricted-auth-permission"]:
            params = (
                f"{params} --cluster-name {config.ENV_DATA['cluster_name']} --cephfs-filesystem-name "
                f"{ceph_fs_name} --restricted-auth-permission true"
            )

        out = self.run_exporter_script(params=params)

        # encode the exporter script output to base64
        external_cluster_details = encode(out)
        logger.debug(f"Encoded external cluster details: {external_cluster_details}")

        # update the encoded message to config
        config.EXTERNAL_MODE["external_cluster_details"] = external_cluster_details

    def upload_exporter_script(self):
        """
        Upload exporter script to RHCS cluster

        Returns:
            str: absolute path to exporter script

        """
        script_path = generate_exporter_script()
        upload_file(self.host, script_path, script_path, self.user, self.password)
        return script_path

    def get_admin_keyring(self):
        """
        Fetches admin keyring from external RHCS cluster and updates to config.EXTERNAL_MODE
        """
        cmd = "ceph auth get client.admin"
        _, out, _ = self.rhcs_conn.exec_cmd(cmd)
        client_admin = out.split()
        for index, value in enumerate(client_admin):
            if value == "key":
                config.EXTERNAL_MODE["admin_keyring"]["key"] = client_admin[index + 2]
                return

    def get_rgw_endpoint_api_port(self):
        """
        Fetches rgw endpoint api port.

        For ceph 5.x, get port information from ceph config dump and for
        ceph 4.x, get port information from ceph.conf on rgw node

        Returns:
            str: RGW endpoint port

        """
        port = None
        try:
            # For ceph 5.x versions
            cmd = "ceph config dump -f json"
            _, out, _ = self.rhcs_conn.exec_cmd(cmd)
            config_dump = json.loads(out)
            for each in config_dump:
                if each["name"].lower() == "rgw_frontends":
                    port = each["value"].split("=")[-1]
                    break
            # if port doesn't have value, need to check ceph.conf from rgw node
            if not port:
                raise AttributeError(
                    "config dump has no rgw port information. checking ceph.conf file on rgw node"
                )
        except Exception as ex:
            # For ceph 4.x versions
            logger.info(ex)
            cmd = "grep -e '^rgw frontends' /etc/ceph/ceph.conf"
            rgw_node = get_rgw_endpoint()
            rgw_conn = Connection(host=rgw_node, user=self.user, password=self.password)
            _, out, _ = rgw_conn.exec_cmd(cmd)
            port = out.split(":")[-1]

        if not port:
            raise ExternalClusterRGWEndPointPortMissing

        logger.info(f"External cluster rgw endpoint api port: {port}")
        return port

    def get_rhel_version(self):
        """
        Fetches the RHEL version on external RHCS cluster

        Returns:
            str: RHEL version

        """
        pattern = re.compile(r".*(\d+.\d+).*")
        cmd = "cat /etc/redhat-release"
        _, out, _ = self.rhcs_conn.exec_cmd(cmd)
        logger.debug(f"RHEL version on external RHCS cluster is {out}")
        return pattern.search(out).groups()[0]

    def update_permission_caps(self, user=None):
        """
        Update permission caps on the external RHCS cluster
        """
        if user:
            params = f"--upgrade --run-as-user={user}"
        else:
            params = "--upgrade"
        out = self.run_exporter_script(params=params)
        logger.info(f"updated permissions for the user are set as {out}")

    def run_exporter_script(self, params):
        """
        Runs the exporter script on RHCS cluster

        Args:
            params (str): Parameter to pass to exporter script

        Returns:
            str: output of exporter script

        """
        # upload exporter script to external RHCS cluster
        script_path = self.upload_exporter_script()

        # get external RHCS rhel version
        rhel_version = self.get_rhel_version()
        python_version = "python3"
        if version.get_semantic_version(rhel_version) < version.get_semantic_version(
            "8"
        ):
            python_version = "python"

        # run the exporter script on external RHCS cluster
        cmd = f"{python_version} {script_path} {params}"
        retcode, out, err = self.rhcs_conn.exec_cmd(cmd)
        if retcode != 0:
            logger.error(
                f"Failed to run {script_path} with parameters {params}. Error: {err}"
            )
            raise ExternalClusterExporterRunFailed
        return out

    def create_object_store_user(self):
        """
        Create object store user on external cluster and update
        access_key and secret_key to config
        """
        # check if object store user exists or not
        user = defaults.EXTERNAL_CLUSTER_OBJECT_STORE_USER
        if self.is_object_store_user_exists(user):
            logger.info(f"object store user {user} already exists in external cluster")
            # get the access and secret key
            access_key, secret_key = self.get_object_store_user_secrets(user)
        else:
            # create new object store user
            logger.info(f"creating new object store user {user}")
            cmd = (
                f"radosgw-admin user create --uid {user} --display-name "
                f'"Rook RGW Admin Ops user" --caps "buckets=*;users=*;usage=read;metadata=read;zone=read"'
            )
            retcode, out, err = self.rhcs_conn.exec_cmd(cmd)
            if retcode != 0:
                logger.error(f"Failed to create object store user. Error: {err}")
                raise ExternalClusterObjectStoreUserCreationFailed

            # get the access and secret key
            objectstore_user_details = json.loads(out)
            access_key = objectstore_user_details["keys"][0]["access_key"]
            secret_key = objectstore_user_details["keys"][0]["secret_key"]

        # update access_key and secret_key in config.EXTERNAL_MODE
        config.EXTERNAL_MODE["access_key_rgw-admin-ops-user"] = access_key
        config.EXTERNAL_MODE["secret_key_rgw-admin-ops-user"] = secret_key

    def is_object_store_user_exists(self, user):
        """
        Checks whether user exists in external cluster

        Returns:
            bool: True if user exists, otherwise false

        """
        cmd = "radosgw-admin user list"
        _, out, _ = self.rhcs_conn.exec_cmd(cmd)
        objectstore_user_list = json.loads(out)
        if user in objectstore_user_list:
            return True

    def get_object_store_user_secrets(self, user):
        """
        Get the access and secret key for user

        Returns:
            tuple: tuple which contains access_key and secret_key

        """
        cmd = f"radosgw-admin user info --uid {user}"
        _, out, _ = self.rhcs_conn.exec_cmd(cmd)
        user_details = json.loads(out)
        return (
            user_details["keys"][0]["access_key"],
            user_details["keys"][0]["secret_key"],
        )

    def get_ceph_fs(self):
        """
        Fetches the ceph filesystem name

        Returns:
            str: ceph filesystem name

        Raises:
            ExternalClusterCephfsMissing: in case of ceph filesystem doesn't exist

        """
        cmd = "ceph fs ls --format json"
        _, out, _ = self.rhcs_conn.exec_cmd(cmd)
        ceph_fs_list = json.loads(out)
        if not ceph_fs_list:
            raise ExternalClusterCephfsMissing
        return ceph_fs_list[0]["name"]


def generate_exporter_script():
    """
    Generates exporter script for RHCS cluster

    Returns:
        str: path to the exporter script

    """
    logger.info("generating external exporter script")
    # generate exporter script through packagemanifest
    ocs_operator_name = defaults.OCS_OPERATOR_NAME
    operator_selector = get_selector_for_ocs_operator()
    package_manifest = PackageManifest(
        resource_name=ocs_operator_name,
        selector=operator_selector,
    )
    ocs_operator_data = package_manifest.get()
    csv = get_ocs_csv()
    for each_csv in ocs_operator_data["status"]["channels"]:
        if each_csv["currentCSV"] == csv.resource_name:
            logger.info(f"exporter script for csv: {each_csv['currentCSV']}")
            encoded_script = each_csv["currentCSVDesc"]["annotations"][
                "external.features.ocs.openshift.io/export-script"
            ]
            break

    # decode the exporter script and write to file
    external_script = decode(encoded_script)
    external_cluster_details_exporter = tempfile.NamedTemporaryFile(
        mode="w+",
        prefix="external-cluster-details-exporter-",
        suffix=".py",
        delete=False,
    )
    with open(external_cluster_details_exporter.name, "w") as fd:
        fd.write(external_script)
    logger.info(
        f"external cluster script is located at {external_cluster_details_exporter.name}"
    )

    return external_cluster_details_exporter.name


def get_rgw_endpoint():
    """
    Fetches rgw endpoint

    Returns:
        str: rgw endpoint

    Raises:
        ExternalClusterRGWEndPointMissing: in case of rgw endpoint missing

    """
    rgw_endpoint = None
    for each in config.EXTERNAL_MODE["external_cluster_node_roles"].values():
        if "rgw" in each["role"]:
            if config.EXTERNAL_MODE.get("use_fqdn_rgw_endpoint"):
                logger.info("using FQDN as rgw endpoint")
                rgw_endpoint = each["hostname"]
            elif config.EXTERNAL_MODE.get("use_ipv6_rgw_endpoint"):
                logger.info("using IPv6 as rgw endpoint")
                rgw_endpoint = each["ipv6_address"]
            else:
                logger.info("using IPv4 as rgw endpoint")
                rgw_endpoint = each["ip_address"]
            return rgw_endpoint
    if not rgw_endpoint:
        raise ExternalClusterRGWEndPointMissing


def get_external_cluster_client():
    """
    Finding the client role node IP address.

    Returns:
        tuple: IP address, user, password of the client
    """
    user = config.EXTERNAL_MODE["login"]["username"]
    password = config.EXTERNAL_MODE["login"]["password"]
    nodes = config.EXTERNAL_MODE["external_cluster_node_roles"]
    for each in nodes.values():
        if "client" in each["role"]:
            return (each["ip_address"], user, password)
    logger.warning("No client role defined, using node1 address!")
    return (nodes["node1"]["ip_address"], user, password)
