#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

r"""## Overview.

This document explains how to integrate a workload charm that needs to send logs to a
charmed operator that implements the `loki_push_api` relation interface to expose
to other charmed operators Loki's Push API endpoint.


Filtering of logs in Loki is largely performed on the basis of labels.
In the Juju ecosystem, Juju topology labels are used to uniquely identify the workload that
generates telemetry like logs.
In order to be able to control the labels on the logs pushed to a Loki Push API endpoint to add
Juju topology labels, this library will create and manage a sidecar container that runs `promtail`
as a logging proxy, injecting Juju topology labels into the logs on the fly.




## Consumer Library Usage

Let's say that we have a workload charm that produce logs and we need to send those logs to a
workload implementing the `loki_push_api` interface, like `Loki` or `Grafana Agent`.

Adopting this library in a charmed operator consist of two steps:


1. Use the `LogProxyConsumer` class by instanting it in the `__init__` method of the
   charmed operator:

   ```python
   from charms.grafana_agent_k8s.v0.grafana_agent import LogProxyConsumer

   ...

       def __init__(self, *args):
           ...
           self._log_proxy = LogProxyConsumer(self, LOG_FILES)
   ```

   Note that `LOG_FILES` is a `list` containing the log files we want to send to `Loki` or
   `Grafana Agent`, for instance:

   ```python
   LOG_FILES = [
       "/var/log/apache2/access.log",
       "/var/log/alternatives.log",
   ]
   ```

2. Modify the `metadata.yaml` file to add:

   - The promtail side-car container:
      ```yaml
        containers:
          promtail:
            resource: promtail-image
      ```

   - The `log_proxy` relation in the `requires` section:
     ```yaml
     requires:
       log_proxy:
         interface: loki_push_api
         optional: true
     ```

   - The `promtail-image` in the `resources` section:
     ```yaml
       resources:
         promtail-image:
           type: oci-image
           description: upstream docker image for Promtail
     ```
"""

import json
import logging
from hashlib import sha256
from typing import Optional
from urllib.request import urlopen
from zipfile import ZipFile

import yaml
from ops.charm import CharmBase, RelationChangedEvent, RelationDepartedEvent
from ops.framework import Object, StoredState

logger = logging.getLogger(__name__)
# The unique Charmhub library identifier, never change it
LIBID = "Qwerty123"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1
PROMTAIL_BINARY_ZIP_URL = (
    "https://github.com/grafana/loki/releases/download/v2.4.1/promtail-linux-amd64.zip"
)
BINARY_ZIP_PATH = "/tmp/promtail-linux-amd64.zip"
BINARY_DIR = "/tmp"
BINARY_SHA25SUM = "978391a174e71cfef444ab9dc012f95d5d7eae0d682eaf1da2ea18f793452031"
WORKLOAD_BINARY_PATH = "/tmp/promtail-linux-amd64"
SERVICE_NAME = "promtail"

DEFAULT_RELATION_NAME = "log_proxy"
HTTP_LISTEN_PORT = 9080
HTTP_LISTEN_PORT = 9080
GRPC_LISTEN_PORT = 0
POSITIONS_FILENAME = "/tmp/positions.yaml"
CONFIG_PATH = "/tmp/promtail_config.yml"


class PromtailDigestError(Exception):
    """Raised if there is an error with Promtail binary file."""


class RelationManagerBase(Object):
    """Base class that represents relation ends ("provides" and "requires").

    :class:`RelationManagerBase` is used to create a relation manager. This is done by inheriting
    from :class:`RelationManagerBase` and customising the sub class as required.

    Attributes:
        name (str): consumer's relation name
    """

    def __init__(self, charm: CharmBase, relation_name=DEFAULT_RELATION_NAME):
        super().__init__(charm, relation_name)
        self._relation_name = relation_name


class LogProxyConsumer(RelationManagerBase):
    """LogProxyConsumer class."""

    _stored = StoredState()

    def __init__(
        self,
        charm,
        log_files: list,
        container_name: Optional[str],
        relation_name: str = DEFAULT_RELATION_NAME,
    ):
        super().__init__(charm, relation_name)
        self._stored.set_default(grafana_agents="{}")
        self._charm = charm
        self._relation_name = relation_name
        self._container_name = container_name
        self._container = self._get_container(container_name)
        self._log_files = log_files
        self.framework.observe(
            self._charm.on.log_proxy_relation_created, self._on_log_proxy_relation_created
        )
        self.framework.observe(
            self._charm.on.log_proxy_relation_changed, self._on_log_proxy_relation_changed
        )
        self.framework.observe(
            self._charm.on.log_proxy_relation_departed, self._on_log_proxy_relation_departed
        )
        self.framework.observe(self._charm.on.upgrade_charm, self._on_upgrade_charm)

    def _on_log_proxy_relation_created(self, event):
        """Event handler for the `log_proxy_relation_created`."""
        self._container.push(CONFIG_PATH, yaml.dump(self._initial_config))

    def _on_log_proxy_relation_changed(self, event):
        """Event handler for the `log_proxy_relation_changed`.

        Args:
            event: The event object `RelationChangedEvent`.
        """
        if event.relation.data[event.unit].get("data", None):
            self._obtain_promtail(event)
            self._update_config(event)
            self._update_agents_list(event)
            self._add_pebble_layer()
            self._container.restart(self._container_name)
            self._container.restart(SERVICE_NAME)

    def _on_log_proxy_relation_departed(self, event):
        """Event handler for the `log_proxy_relation_departed`.

        Args:
            event: The event object `RelationDepartedEvent`.
        """
        self._update_config(event)
        self._update_agents_list(event)

        if len(self._current_config["clients"]) == 0:
            self._container.stop(SERVICE_NAME)
        else:
            self._container.restart(SERVICE_NAME)

    def _on_upgrade_charm(self, event):
        # TODO: Implement it ;-)
        pass

    def _get_container(self, container_name):
        if container_name is not None:
            return self._charm.unit.get_container(container_name)

        containers = dict(self._charm.model.unit.containers)

        if len(containers) == 1:
            return self._charm.unit.get_container([*containers].pop())

        # FIXME: Use custom exception
        raise Exception("Container cannot be obtained")

    def _add_pebble_layer(self):
        pebble_layer = {
            "summary": "promtail layer",
            "description": "pebble config layer for promtail",
            "services": {
                SERVICE_NAME: {
                    "override": "replace",
                    "summary": SERVICE_NAME,
                    "command": "{} {}".format(WORKLOAD_BINARY_PATH, self._cli_args),
                    "startup": "enabled",
                }
            },
        }
        self._container.add_layer(self._container_name, pebble_layer, combine=True)

    def _obtain_promtail(self, event) -> None:
        if self._is_promtail_binary_in_workload():
            return

        self._download_promtail(event)

        if not self._check_sha256sum():
            msg = "Promtail bnary sha256sum mismatch"
            logger.error(msg)
            raise PromtailDigestError(msg)

        self._unzip_binary()
        self._upload_binary()

    def _is_promtail_binary_in_workload(self) -> bool:
        pat = WORKLOAD_BINARY_PATH.split("/")[-1]
        return True if len(self._container.list_files(BINARY_DIR, pattern=pat)) == 1 else False

    def _download_promtail(self, event) -> None:
        url = json.loads(event.relation.data[event.unit].get("data"))["promtail_binary_zip_url"]
        response = urlopen(url)

        with open(BINARY_ZIP_PATH, "wb") as f:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                f.write(chunk)

    def _check_sha256sum(self, filename=BINARY_ZIP_PATH, sha256sum=BINARY_SHA25SUM) -> bool:
        with open(filename, "rb") as f:
            f_byte = f.read()
            result = sha256(f_byte).hexdigest()

        if sha256sum == result:
            return True

        return False

    def _unzip_binary(self, zip_file=BINARY_ZIP_PATH, binary_dir=BINARY_DIR) -> None:
        with ZipFile(zip_file, "r") as zip_ref:
            zip_ref.extractall(binary_dir)

    def _upload_binary(self, dest=WORKLOAD_BINARY_PATH, origin=WORKLOAD_BINARY_PATH) -> None:
        with open(origin, "rb") as f:
            self._container.push(dest, f, permissions=0o755)

    def _update_agents_list(self, event):
        """Updates the active Grafana agents list.

        Args:
            event: The event object `RelationChangedEvent` or `RelationDepartedEvent`
        """
        grafana_agents = json.loads(self._stored.grafana_agents)

        if isinstance(event, RelationChangedEvent):
            agent_url = json.loads(event.relation.data[event.unit].get("data"))["loki_push_api"]
            grafana_agents[str(event.unit)] = agent_url
            self._stored.grafana_agents = json.dumps(grafana_agents)

        if isinstance(event, RelationDepartedEvent):
            agent_url = grafana_agents.pop(str(event.unit))
            self._stored.grafana_agents = json.dumps(grafana_agents)

    def _update_config(self, event):
        """Updates the config file for Promtail and upload it to the side-car container.

        Args:
            event: `RelationChangedEvent` or `RelationDepartedEvent`
        """
        config = self._build_config_file(event)
        self._container.push(CONFIG_PATH, config)

    @property
    def _cli_args(self) -> str:
        """Return the cli arguments to pass to promtail.

        Returns:
            The arguments as a string
        """
        return "-config.file={}".format(CONFIG_PATH)

    @property
    def _current_config(self) -> dict:
        """Property that returns the current Promtail configuration.

        Returns:
            A dict containing Promtail configuration.
        """
        raw_current = self._container.pull(CONFIG_PATH).read()
        current_config = yaml.safe_load(raw_current)
        return current_config

    def _build_config_file(self, event) -> str:
        """Generates config file str based on the event received.

        Args:
            event: `RelationChangedEvent` or `RelationDepartedEvent`

        Returns:
            A yaml string with Promtail config.
        """
        config = {}
        if isinstance(event, RelationChangedEvent):
            agent_url = json.loads(event.relation.data[event.unit].get("data"))["loki_push_api"]
            config = self._add_client(self._current_config, agent_url)

        if isinstance(event, RelationDepartedEvent):
            agent_url = json.loads(self._stored.grafana_agents)[str(event.unit)]
            config = self._remove_client(self._current_config, agent_url)

        return yaml.dump(config)

    @property
    def _initial_config(self) -> dict:
        """Generates an initial config for Promtail.

        This config it's going to be completed with the `client` section
        once a relation between Grafana Agent charm and a workload charm is established.
        """
        config = {}
        config.update(self._server_config())
        config.update(self._positions())
        config.update(self._scrape_configs())
        return config

    def _add_client(self, current_config: dict, agent_url: str) -> dict:
        """Updates Promtail's current configuration by adding a Grafana Agent URL.

        Args:
            current_config: A dictionary containing Promtail current configuration.
            agent_url: A string with Grafana Agent URL.

        Returns:
            Updated Promtail configuration.
        """
        if "clients" in current_config:
            current_config["clients"].append({"url": agent_url})
        else:
            current_config["clients"] = [{"url": agent_url}]

        return current_config

    def _remove_client(self, current_config, agent_url) -> dict:
        """Updates Promtail's current configuration by removing a Grafana Agent URL.

        Args:
            current_config: A dictionary containing Promtail current configuration.
            agent_url: A string with Grafana Agent URL.

        Returns:
            Updated Promtail configuration.
        """
        if clients := current_config.get("clients"):
            clients = [c for c in clients if c != {"url": agent_url}]
            current_config["clients"] = clients
            return current_config

        return current_config

    def _server_config(self) -> dict:
        """Generates the server section of the Promtail config file.

        Returns:
            The dict representing the `server` section.
        """
        return {
            "server": {
                "http_listen_port": HTTP_LISTEN_PORT,
                "grpc_listen_port": GRPC_LISTEN_PORT,
            }
        }

    def _positions(self) -> dict:
        """Generates the positions section of the Promtail config file.

        Returns:
            The dict representing the `positions` section.
        """
        return {"positions": {"filename": POSITIONS_FILENAME}}

    def _scrape_configs(self) -> dict:
        """Generates the scrape_configs section of the Promtail config file.

        Returns:
            The dict representing the `scrape_configs` section.
        """
        # TODO: We need to define the right values for:
        # - job_name
        # - targets
        # - __path__
        #
        # Also we need to use the log_files that we get from the consumer.
        # and use the JujuTopology object
        return {
            "scrape_configs": [
                {
                    "job_name": "system",
                    "static_configs": [
                        {
                            "targets": ["localhost"],
                            "labels": {
                                "job": "juju_{}_{}_{}".format(
                                    self._charm.model.name,
                                    self._charm.model.uuid,
                                    self._charm.model.app.name,
                                ),
                                "__path__": "/var/log/dmesg",
                            },
                        }
                    ],
                }
            ]
        }


class LogProxyProvider(RelationManagerBase):
    """LogProxyProvider class."""

    def __init__(self, charm, relation_name: str = DEFAULT_RELATION_NAME):
        super().__init__(charm, relation_name)
        self._charm = charm
        self._relation_name = relation_name
        self.framework.observe(
            self._charm.on.log_proxy_relation_changed, self._on_log_proxy_relation_changed
        )
        self.framework.observe(self._charm.on.upgrade_charm, self._on_upgrade_charm)

    def _on_log_proxy_relation_changed(self, event):
        if event.relation.data[self._charm.unit].get("data") is None:
            data = {}
            data.update(json.loads(self._loki_push_api))
            data.update(json.loads(self._promtail_binary_url))
            event.relation.data[self._charm.unit].update({"data": json.dumps(data)})

    def _on_upgrade_charm(self, event):
        pass

    @property
    def _promtail_binary_url(self) -> str:
        # FIXME: Use charmhub's URL
        return json.dumps({"promtail_binary_zip_url": PROMTAIL_BINARY_ZIP_URL})

    @property
    def _loki_push_api(self) -> str:
        """Fetch Loki push API URL.

        Returns:
            Loki push API URL as json string
        """
        loki_push_api = "http://{}:{}/loki/api/v1/push".format(
            self.unit_ip, self._charm._http_listen_port
        )
        data = {"loki_push_api": loki_push_api}
        return json.dumps(data)

    @property
    def unit_ip(self) -> str:
        """Returns unit's IP."""
        if bind_address := self._charm.model.get_binding(self._relation_name).network.bind_address:
            return str(bind_address)
        return ""
