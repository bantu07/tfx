# Lint as: python2, python3
# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Model server runner for kubernetes runtime."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import datetime
import time
from typing import Text

from absl import logging
from kubernetes import client as k8s_client
from kubernetes.client import rest

from tfx.components.infra_validator import error_types
from tfx.components.infra_validator import serving_bins
from tfx.components.infra_validator.model_server_runners import base_runner
from tfx.proto import infra_validator_pb2
from tfx.utils import kube_utils
from tfx.utils import time_utils

_DEFAULT_POLLING_INTERVAL_SEC = 5
_DEFAULT_ACTIVE_DEADLINE_SEC = int(datetime.timedelta(hours=24).total_seconds())

# Kubernetes resource metadata values
_APP_KEY = 'app'
_MODEL_SERVER_POD_NAME_PREFIX = 'tfx-infraval-modelserver-'
_MODEL_SERVER_APP_LABEL = 'tfx-infraval-modelserver'
_MODEL_SERVER_CONTAINER_NAME = 'model-server'

# Phases of the pod as described in
# https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-phase.
_POD_PHASE_RUNNING = 'Running'
_POD_PHASE_SUCCEEDED = 'Succeeded'
_POD_PHASE_FAILED = 'Failed'

# PodSpec container restart policy as described in
# https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#restart-policy
_POD_CONTAINER_RESTART_POLICY_NEVER = 'Never'


class KubernetesRunner(base_runner.BaseModelServerRunner):
  """A model server runner that launches model server in kubernetes cluster."""

  def __init__(
      self,
      model_path: Text,
      serving_binary: serving_bins.ServingBinary,
      serving_spec: infra_validator_pb2.ServingSpec):
    """Create a kubernetes model server runner.

    Args:
      model_path: An IV-flavored model path. (See model_path_utils.py)
      serving_binary: A ServingBinary to run.
      serving_spec: A ServingSpec instance.
    """
    assert serving_spec.WhichOneof('serving_platform') == 'kubernetes', (
        'ServingSpec configuration mismatch.')
    self._config = serving_spec.kubernetes

    self._model_path = model_path
    self._serving_binary = serving_binary
    self._serving_spec = serving_spec
    self._k8s_core_api = kube_utils.make_core_v1_api()
    if not kube_utils.is_inside_kfp():
      raise NotImplementedError(
          'KubernetesRunner should be running inside KFP.')
    self._executor_pod = kube_utils.get_current_kfp_pod(self._k8s_core_api)
    self._namespace = kube_utils.get_kfp_namespace()
    self._label_dict = {
        _APP_KEY: _MODEL_SERVER_APP_LABEL,
    }
    # Pod name would be populated once creation request sent.
    self._pod_name = None
    # Endpoint would be populated once the Pod is running.
    self._endpoint = None

  def __repr__(self):
    return 'KubernetesRunner(image: {image}, pod_name: {pod_name})'.format(
        image=self._serving_binary.image,
        pod_name=self._pod_name)

  def GetEndpoint(self) -> Text:
    assert self._endpoint is not None, (
        'self._endpoint is not ready. You should call Start() and '
        'WaitUntilRunning() first.')
    return self._endpoint

  def Start(self) -> None:
    assert not self._pod_name, (
        'You cannot start model server multiple times.')

    # We're creating a Pod rather than a Deployment as we're relying on
    # executor's retry mechanism for failure recovery, and the death of the Pod
    # should be regarded as a validation failure.
    pod = self._k8s_core_api.create_namespaced_pod(
        namespace=self._namespace,
        body=self._BuildPodManifest())
    self._pod_name = pod.metadata.name
    logging.info('Created Pod:\n%s', pod)

  def WaitUntilRunning(self, deadline: float) -> None:
    assert self._pod_name, (
        'Pod has not been created yet. You should call Start() first.')

    while time.time() < deadline:
      try:
        pod = self._k8s_core_api.read_namespaced_pod(
            name=self._pod_name,
            namespace=self._namespace)
      except rest.ApiException as e:
        logging.info('Continue polling after getting ApiException(%s)', e)
        time.sleep(_DEFAULT_POLLING_INTERVAL_SEC)
        continue
      # Pod phase is one of Pending, Running, Succeeded, Failed, or Unknown.
      # Succeeded and Failed indicates the pod lifecycle has reached its end,
      # while we expect the job to be running and hanging. Phase is Unknown if
      # the state of the pod could not be obtained, thus we can wait until we
      # confirm the phase.
      pod_phase = pod.status.phase
      if pod_phase == _POD_PHASE_RUNNING and pod.status.pod_ip:
        self._endpoint = '{}:{}'.format(pod.status.pod_ip,
                                        self._serving_binary.container_port)
        return
      if pod_phase in (_POD_PHASE_SUCCEEDED, _POD_PHASE_FAILED):
        raise error_types.JobAborted(
            'Job has been aborted. (phase={})'.format(pod_phase))
      logging.info('Waiting for the pod to be running. (phase=%s)', pod_phase)
      time.sleep(_DEFAULT_POLLING_INTERVAL_SEC)

    raise error_types.DeadlineExceeded(
        'Deadline exceeded while waiting for pod to be running.')

  def Stop(self) -> None:
    for _ in time_utils.exponential_backoff(attempts=5):
      try:
        logging.info('Deleting Pod (name=%s)', self._pod_name)
        self._k8s_core_api.delete_namespaced_pod(
            name=self._pod_name,
            namespace=self._namespace)
        return
      except rest.ApiException as e:
        if e.status == 404:
          logging.info('Pod (name=%s) does not exist.', self._pod_name)
          return
        logging.warning('Error occured while deleting the Pod (name=%s).',
                        self._pod_name)
        logging.exception(e)

    # All the exponential backoff was unsuccessful.
    logging.warning(
        'Unable to delete the model server Pod.\n'
        'Please run the following command to manually clean up the resource\n'
        '\n'
        'kubectl delete pod --namespace %s %s\n'
        '\n',
        self._namespace,
        self._pod_name)

  def _BuildPodManifest(self) -> k8s_client.V1Pod:
    if isinstance(self._serving_binary, serving_bins.TensorFlowServing):
      env_vars_dict = self._serving_binary.MakeEnvVars(
          model_path=self._model_path)
      env_vars = [k8s_client.V1EnvVar(name=key, value=value)
                  for key, value in env_vars_dict.items()]
    else:
      raise NotImplementedError('Unsupported serving binary {}'.format(
          type(self._serving_binary).__name__))

    service_account_name = (self._config.service_account_name or
                            self._executor_pod.spec.service_account_name)
    active_deadline_seconds = (self._config.active_deadline_seconds or
                               _DEFAULT_ACTIVE_DEADLINE_SEC)
    if active_deadline_seconds < 0:
      raise ValueError('active_deadline_seconds should be > 0. Got {}'
                       .format(active_deadline_seconds))

    return k8s_client.V1Pod(
        metadata=k8s_client.V1ObjectMeta(
            generate_name=_MODEL_SERVER_POD_NAME_PREFIX,
            labels=self._label_dict,
            # Resources with ownerReferences are automatically deleted once all
            # its owners are deleted.
            owner_references=[
                k8s_client.V1OwnerReference(
                    api_version=self._executor_pod.api_version,
                    kind=self._executor_pod.kind,
                    name=self._executor_pod.metadata.name,
                    uid=self._executor_pod.metadata.uid,
                ),
            ],
        ),
        spec=k8s_client.V1PodSpec(
            containers=[
                k8s_client.V1Container(
                    name=_MODEL_SERVER_CONTAINER_NAME,
                    image=self._serving_binary.image,
                    env=env_vars,
                ),
            ],
            service_account_name=service_account_name,
            # No retry in case model server container failed. Retry will happen
            # at the outermost loop (executor.py).
            restart_policy=_POD_CONTAINER_RESTART_POLICY_NEVER,
            # This is a hard deadline for the model server container to ensure
            # the Pod is properly cleaned up even with an unexpected termination
            # of an infra validator. After the deadline, container will be
            # removed but Pod resource won't. This makes the Pod log visible
            # after the termination.
            active_deadline_seconds=active_deadline_seconds,
            # TODO(b/152002076): Add TTL controller once it graduates Beta.
            # ttl_seconds_after_finished=,
        )
    )