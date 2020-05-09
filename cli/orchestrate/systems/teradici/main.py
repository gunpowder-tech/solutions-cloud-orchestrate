# python3
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deploy Teradici Cloud Access Software including broker and security gateway.
"""

import logging
import os
import tempfile
from . import camapi
from orchestrate import base


log = logging.getLogger(__name__)


class CloudAccessSoftware(base.OrchestrateSystem):
  """Deploy Teradici CAS."""

  def __init__(self):
    super(CloudAccessSoftware, self).__init__()
    # Active Directory
    self.domain = 'demo'
    self.users_file = ''

    # CAM
    self.api_token = None
    self.registration_code = None
    self.deployment_name = None
    self.connector_name = None

    # SSH
    self.public_ssh_key_file = None

    # Network
    self.network = 'workstations'
    self.workstations_cidr = '10.0.0.0/20'
    self.controller_cidr = '10.0.240.0/21'
    self.controller_ip = '10.0.240.2'
    self.connector_cidr = '10.0.248.0/21'

    # Windows workstations
    self.windows_instance_count = 0
    self.windows_instance_name = 'win'
    self.windows_image = 'projects/windows-cloud/global/images/family/windows-2019'
    self.windows_disk_size = 200
    self.windows_machine_type = 'n1-standard-8'
    self.windows_accelerator_type = None
    self.windows_accelerator_count = 1

    # Overrides
    self.git_url = 'https://github.com/teradici/cloud_deployment_scripts'
    self.git_branch = 'master'
    self.deploy_dir = ''
    self.terraform_version = '0.12.7'

  @property
  def description(self):
    return """Deploys Teradici CAS along with a standalone Active Directory."""

  def run(self):
    """Executes system deployment.

    Returns:
      True if successful. False, otherwise.
    """
    log.info('Deploying Teradici CAS')

    self.enable_apis()
    self.create_connector_token()
    self.create_ssh_keys()

    roles = """
roles/editor
roles/cloudkms.cryptoKeyEncrypterDecrypter
""".strip().split()
    self.create_service_account(roles)
    self.create_service_account_key()

    self.install_terraform()
    self.configure_terraform()
    self.apply_terraform()

    self.remove_service_account_key()

  def configure(self):
    """Configure."""
    self.region = '-'.join(self.zone.split('-')[:-1])

    self.terraform_version = '0.12.7'

    if self.deploy_dir:
      command = 'mkdir -p {self.deploy_dir}'.format(self=self)
      self.run_command(command)
    else:
      self.deploy_dir = directory = tempfile.mkdtemp(
          prefix='orchestrate-{self.project}-{self.name}-'.format(self=self),
          dir='/var/tmp',
          )

    self.service_account_name = 'teradici'
    self.service_account_display_name = 'Teradici CAS'
    self.service_account = (
        '{self.service_account_name}@{self.project}.iam.gserviceaccount.com'
    ).format(self=self)
    self.credentials_file = (
        '{self.deploy_dir}/{self.project}-{self.service_account_name}.json'
    ).format(self=self)

    self.terraform_dir = '{self.deploy_dir}/{self.name}'.format(self=self)
    self.terraform_deployment_dir = (
        '{self.terraform_dir}/deployments/gcp/single-connector'
        ).format(self=self)

    if not self.public_ssh_key_file:
      self.public_ssh_key_file = (
          '{self.deploy_dir}/{self.project}-{self.service_account_name}'
          ).format(self=self)

    self.connector_token = None

    if self.windows_accelerator_type is None:
      if self.region in ['us-west2', 'us-east4', 'northamerica-northeast1']:
        self.windows_accelerator_type = 'nvidia-tesla-p4-vws'
      else:
        self.windows_accelerator_type = 'nvidia-tesla-t4-vws'

  def create_connector_token(self):
    """Create a CAM connector token for the deployment."""
    log.info('Creating connector token')
    deployment_name = self.deployment_name
    if not deployment_name:
      if self.prefix:
        deployment_name = '{}-{}'.format(self.project, self.prefix)
      else:
        deployment_name = self.project
    connector_name = self.connector_name or self.prefix or self.zone
    log.info('deployment: %s', deployment_name)
    log.info('connector : %s', connector_name)
    if self.dry_run:
      log.info('DRY-RUN get_connector_token')
      self.connector_token = None
      return
    self.connector_token = self.get_connector_token(
        deployment_name,
        connector_name,
        self.registration_code,
        self.api_token,
        )

  def get_connector_token(self, deployment_name, connector_name,
                          registration_code, api_token):
    """Returns a valid token for a Teradici CAM connector.

    Args:
      deployment_name:
      connector_name:
      registration_code:
      api_token:
    """
    cam = camapi.CloudAccessManager(api_token)

    # Get or create Deployment
    deployment = cam.deployments.get(deployment_name)
    if not deployment:
      deployment = cam.deployments.post(deployment_name, registration_code)

    # Get an authorization token for the connector that the broker will use
    connector_token = cam.auth.tokens.connector.post(deployment, connector_name)
    return connector_token

  def enable_apis(self):
    """Enable APIs."""
    log.info('Enabling APIs')
    command = (
        'gcloud services enable'
        ' cloudkms.googleapis.com'
        ' cloudresourcemanager.googleapis.com'
        ' compute.googleapis.com'
        ' dns.googleapis.com'
        ' deploymentmanager.googleapis.com'
        )
    self.run_command(command)

  def create_ssh_keys(self):
    """Create SSH keys."""
    log.info('Generating SSH keys')
    if not os.path.exists(self.public_ssh_key_file):
      command = 'ssh-keygen -f {self.public_ssh_key_file} -t rsa -q -N ""'.format(
          self=self,
          )
      self.run_command(command)
    else:
      log.info('Reusing existing SSH key at %s', self.public_ssh_key_file)

  def get_terraform_configuration(self):
    """Returns string with the contents of the tfvars to write."""
    log.info('Configuring Terraform')
    return """
# Project
gcp_credentials_file = "{self.credentials_file}"
gcp_project_id       = "{self.project}"
gcp_service_account  = "{self.service_account}"
gcp_region           = "{self.region}"
gcp_zone             = "{self.zone}"

# Networking
vpc_name             = "{self.network}"
workstations_network = "{self.network}"
controller_network   = "controller"
connector_network    = "connector"
dc_subnet_cidr       = "{self.controller_cidr}"
dc_private_ip        = "{self.controller_ip}"
cac_subnet_cidr      = "{self.connector_cidr}"
ws_subnet_cidr       = "{self.workstations_cidr}"

# Domain
prefix               = "{self.prefix}"
domain_name          = "{self.domain}"
domain_users_list    = "{self.users_file}"

# Access
cac_token                     = "{self.connector_token}"
cac_admin_ssh_pub_key_file    = "{self.public_ssh_key_file}"
centos_admin_ssh_pub_key_file = "{self.public_ssh_key_file}"
dc_admin_password           = "SecuRe_pwd1"
safe_mode_admin_password    = "SecuRe_pwd2"
ad_service_account_password = "SecuRe_pwd3"

# License
pcoip_registration_code  = "{self.registration_code}"

# Workstations
win_gfx_instance_count = {self.windows_instance_count}
win_gfx_instance_name = "{self.windows_instance_name}"
win_gfx_disk_image = "{self.windows_image}"
win_gfx_disk_size_gb = {self.windows_disk_size}
win_gfx_machine_type = "{self.windows_machine_type}"
win_gfx_accelerator_type = "{self.windows_accelerator_type}"
win_gfx_accelerator_count = {self.windows_accelerator_count}

centos_gfx_instance_count = 0
""".lstrip().format(self=self)