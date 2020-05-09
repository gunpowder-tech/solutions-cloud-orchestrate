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

"""Implements the Orchestrate API Service."""

import re
import uuid
from googleapiclient import discovery
from googleapiclient import errors
from oauth2client.client import GoogleCredentials
from google.cloud import error_reporting

import orchestrate_pb2
from orchestrateapi import environ

error_client = error_reporting.Client()

# Connect to Google Cloud Compute Engine API using the environment's service
# account.
credentials = GoogleCredentials.get_application_default()
compute = discovery.build('compute', 'v1', credentials=credentials)


class OrchestrateInstanceCreationError(Exception):
  """Provides detailed message on error occurred during instance creation.
  """
  pass


def run(request, context):
  """Creates an instance.

  Args:
    request (orchestrate_pb2.CreateInstanceRequest): Request payload.
    context: Context.

  Returns:
    A orchestrate_pb2.CreateInstanceResponse with the status of the request.
  """
  instance = request.instance
  print('Orchestrate.CreateInstance project={project} zone={zone}'.format(
      project=instance.project,
      zone=instance.zone,
      ))

  request_id = uuid.uuid4().hex

  try:
    payload = build_instance_payload(instance)
    operation = compute.instances().insert(
        project=instance.project,
        zone=instance.zone,
        body=payload,
        ).execute()
    print('Started operation {name}'.format(name=operation['name']))

    return orchestrate_pb2.CreateInstanceResponse(
        status='SUBMITTED',
        request_id=str(request_id),
        name=payload['name'],
        )

  except errors.HttpError as exception:
    if exception.resp.status == 409:
      message = 'An instance with name {name} already exists.'.format(
          name=payload['name'])
      raise OrchestrateInstanceCreationError(message)
    else:
      raise


def build_instance_payload(instance):
  """Returns a dict with all creation parameters.

  Payload format required by the POST instances.insert endpoint.
  https://cloud.google.com/compute/docs/reference/rest/v1/instances/insert

  Args:
    instance: Creation parameters.
  """
  template = get_template(instance)
  instance_metadata, orchestrate_metadata = get_metadata(instance, template)
  name = build_name(instance, orchestrate_metadata)

  region = '-'.join(instance.zone.split('-')[:2])
  region_url = 'projects/{project}/regions/{region}'.format(
      project=instance.project,
      region=region,
      )

  zone_url = 'projects/{project}/zones/{zone}'.format(
      project=instance.project,
      zone=instance.zone,
      )

  properties = template['properties']

  boot_image, boot_image_latest = get_boot_images(properties['disks'])

  # POST https://www.googleapis.com/compute/v1/
  #       projects/{project}/zones/us-central1-a/instances
  payload = dict(
      name=name,
      description=(
          'Orchestrate instance created from template {template} size {size}'
          ).format(template=instance.template, size=instance.size),
      )
  keys_to_transfer = [
      'machineType',
      'tags',
      'canIpForward',
      'networkInterfaces',
      'labels',
      'scheduling',
      'deletionProtection',
      'serviceAccounts',
      'guestAccelerators',
      'disks',
  ]
  for key in keys_to_transfer:
    if key in properties:
      payload[key] = properties[key]

  # Expand to proper URLs for some values that are not supported as URLs
  # when stored in the instanceTemplate

  # machineType
  # b/137211294 - orchestrate instances create would have to pay attention to
  # this value and override the machineType from this template.
  payload['machineType'] = '{zone_url}/machineTypes/{machine_type}'.format(
      zone_url=zone_url,
      machine_type=orchestrate_metadata.get('machine_type', 'n1-standard-8'),
      )

  # acceleratorType
  for accelerator in payload.get('guestAccelerators', []):
    accelerator['acceleratorType'] = (
        '{zone_url}/acceleratorTypes/{gpu_type}'
        ).format(
            zone_url=zone_url,
            gpu_type=accelerator['acceleratorType'],
            )

  # diskType
  for disk in payload['disks']:
    parameters = disk['initializeParams']
    parameters['diskType'] = '{zone_url}/diskTypes/{disk_type}'.format(
        zone_url=zone_url,
        disk_type=parameters['diskType'],
        )
    if disk['boot'] and instance.use_latest_image:
      # Updates reference to sourceImage to the latest in the image family.
      parameters['sourceImage'] = boot_image_latest['selfLink']

  # subnetwork
  for interface in payload['networkInterfaces']:
    network = orchestrate_metadata.get('network', 'default')
    interface['network'] = 'global/networks/{network}'.format(network=network)
    interface['subnetwork'] = '{region_url}/subnetworks/{subnetwork}'.format(
        region_url=region_url,
        subnetwork=network,
        )

  set_startup_script(
      instance_metadata,
      boot_image_latest if instance.use_latest_image else boot_image
      )
  payload['metadata'] = dict(items=instance_metadata)

  return payload


def get_boot_images(disks):
  """Returns the image referenced in the template and the latest for boot disk.

  Args:
    disks: A list of properties.disks as returned by instanceTemplates.get

  Raises:
    OrchestrateInstanceCreationError: if no boot disk can be found.
  """
  for disk in disks:
    if disk['boot']:
      return get_images(disk['initializeParams'])

  raise OrchestrateInstanceCreationError('Template has no boot disk.')


def get_images(parameters):
  """Returns the image referenced in the template and the latest version.

  Args:
    parameters: A dict containing the values of
      properties.disks[].initializeParams as returned by instanceTemplates.get
  """
  # source image example:
  #   projects/cloud-media-solutions/global/images/visual-20191217t184104
  link = parameters['sourceImage']

  # Extract image project and version from that link, e.g.:
  #   project=cloud-media-solutions
  #   version=visual-20191217t184104
  match = re.match(r'.*/projects/(?P<project>.*)/global/images/(?P<version>.*)',
                   link)
  project = match.group('project')
  version = match.group('version')

  # get the family from the source image version
  image = compute.images().get(project=project, image=version).execute()

  # get latest image version from the family
  latest = compute.images().getFromFamily(
      project=project,
      family=image['family'],
      ).execute()

  return image, latest


def get_template(instance):
  """Returns the instanceTemplate for the requested Orchestrate template and size.

  Args:
    instance: Instance creation parameters.

  Raises:
    OrchestrateInstanceCreationError: if no template can be located based on the
      given instance creation parameters.
  """
  if not instance.size:
    print('Locating default size for template {template}'.format(
        template=instance.template))
    response = compute.instanceTemplates().list(
        project=instance.project,
        filter='name = "{}-*"'.format(instance.template),
        ).execute()
    templates = response.get('items', dict())
    for template in templates:
      for item in template['properties']['metadata']['items']:
        if item['key'] == 'orchestrate_default_size' and item['value'] == 'true':
          print('Found instanceTemplate {template}'.format(
              template=template['name']))
          return template
    message = (
        'Could not locate default size for project {project} template'
        ' {template}. Please specify an explicit size in the request.'.format(
            project=instance.project,
            template=instance.template,
            )
        )
    raise OrchestrateInstanceCreationError(message)
  else:
    print('Finding instanceTemplate {template} size {size}'.format(
        template=instance.template, size=instance.size))
    name = '{template}-{size}'.format(
        template=instance.template, size=instance.size)
    template = compute.instanceTemplates().get(
        project=instance.project,
        instanceTemplate=name,
        ).execute()
    return template


def build_name(instance, orchestrate_metadata):
  """Returns an appropriate name for the instance.

  The name is determined in the following order:
  1. Use the one explicitly requested upon creation in the instance object.
  2. Construct one using the instance-name-pattern value in the requested
     template, if any.
  3. Generate a unique name based on the requested template and size.

  Args:
    instance: Instance creation parameters.
    orchestrate_metadata (dict): Orchestrate-specific metadata stored in the
      instanceTemplate object representing the Orchestrate template. The
      instance-name-pattern is stored here, if any.
  """
  # Example pattern: {type}-{region}-{gpu_count}x{gpu_type}-{user}
  instance_name_pattern = orchestrate_metadata.get('instance_name_pattern')
  default_pattern = '{template}-{size}-{id}'
  name_pattern = instance.name or instance_name_pattern or default_pattern
  unique_id = uuid.uuid4().hex[:5]
  gpu_count = orchestrate_metadata.get('gpu_count', 0)
  gpu_type = orchestrate_metadata.get('gpu_type', '')
  graphics_type = 'vws' if gpu_type.endswith('-vws') else 'gpu'
  gpu_name = gpu_type if not gpu_type.endswith('-vws') else gpu_type[:-4]
  region = '-'.join(instance.zone.split('-')[:2])
  name = name_pattern.format(
      template=instance.template,
      size=instance.size,
      region=region,
      zone=instance.zone,
      type=graphics_type,
      gpu_name=gpu_name,
      gpu_count=gpu_count,
      gpu_type=gpu_type,
      user=unique_id,
      id=unique_id,
      )
  return name


def get_metadata(instance, template):
  """Split metadata stored in template in instance and orchestrate dictionaries.

  The instanceTemplate representing the Orchestrate template and size stores two
  kinds of metadata in the same list: One set intended for the instance itself,
  and the other for Orchestrate-specific attributes that extend those stored in the
  instanceTemplate itself. The latter are prefixed with "orchestrate_". This method
  splits the metadata into two groups:

  1. Instance metadata: A list of key,value dictionaries compatible with the
     instances.insert and instanceTemplates.insert API methods.
  2. Orchestrate metadata: A Python dictionary for easier access. The orchestrate_
     prefix is stripped from key name.

  Args:
    instance: Instance creation parameters.
    template: An instanceTemplate object representing the Orchestrate template and
      size requested for the instance.

  Returns:
    A tuple with two dictionaries with the instance and orchestrate-specific
    metadata.
    The instance metadata is in this format [dict(key=..., value=...),...]
    The orchestrate metadata is a Python dictionary.
  """
  instance_metadata = []
  orchestrate_metadata = dict()

  # Order matters.
  # 1. Get metadata from template.
  for item in template['properties']['metadata']['items']:
    if item['key'].startswith('orchestrate_'):
      key = item['key'][8:]
      orchestrate_metadata[key] = item['value']
    else:
      instance_metadata.append(item)

  # 2. Override with metadata explicitly provided upon instance creation.
  for item in instance.metadata:
    instance_metadata.append(dict(key=item.key, value=item.value))

  return instance_metadata, orchestrate_metadata


def set_startup_script(metadata, image):
  """Set startup script to run post-creation configuration based on metadata.

  Determine whether to use a Python startup script for Linux or a PowerShell
  one for Windows depending on the image base OS (see get_os_type)

  Args:
    metadata: Instance metadata
    image: Image.

  Raises:
    OrchestrateInstanceCreationError: if cannot determine the type of OS from the
      given image.
  """
  # TODO(b/126764704) This currently precludes clients from running their own
  # startup script. Need to find out a better/alternate way of doing this.
  # For our current purpose and use case this would suffice.
  os_type = get_os_type(image)

  if os_type == 'linux':
    script = 'gs://{bucket}/remotedesktopconfigure.py'.format(
        bucket=environ.ORCHESTRATE_BUCKET)
    metadata.append(dict(
        key='startup-script-url',
        value=script,
        ))
  elif os_type == 'windows':
    script = 'gs://{bucket}/remotedesktopconfigure.ps1'.format(
        bucket=environ.ORCHESTRATE_BUCKET)
    metadata.append(dict(
        key='windows-startup-script-url',
        value=script,
        ))


def get_os_type(image):
  """Returns the base OS for given image.

  Determine by looking for a "orchestrate_os" label in the image first in case
  this is a custom image. If not, look at the prefix of the family name, e.g.
  stock GCP images start with centos-, windows-, etc.

  Args:
    image: Image.

  Raises:
    OrchestrateInstanceCreationError: if it cannot determine the type of OS.
  """
  # 1. Check for an explicit Orchestrate label.
  labels = image.get('labels', dict())
  os_type = labels.get('orchestrate_os')
  if os_type:
    return os_type.lower()

  # 2. Guess by family name, if possible.
  # Get prefix based on GCP naming conventions, e.g.:
  # centos-7, rhel-7, ubuntu-1804-lts, windows-2016
  # https://cloud.google.com/compute/docs/images
  linux_families = [
      'centos',
      'debian',
      'rhel',
      'sles',
      'cos',
      'coreos',
      'ubuntu',
  ]

  family = image['family'].split('-')[0]

  if family == 'windows':
    return 'windows'
  elif family in linux_families:
    return 'linux'

  # 3. Cannot determine OS
  message = (
      'Image {image} does not have a orchestrate_os label. And, could not guess the'
      ' OS from the family name based on GCP image family naming conventions,'
      ' e.g. windows-, centos-, etc. Please add a orchestrate_os label and set it'
      ' to either "linux" or "windows" to indicate the base OS for this image.'
      ' Or, rename the image family to include a prefix with the base OS name.'
      ).format(image=image['selfLink'])
  raise OrchestrateInstanceCreationError(message)