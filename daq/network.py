"""Networking module"""

from __future__ import absolute_import

from ipaddress import ip_network
import copy
import os
import time
import yaml

import logger
from topology import FaucetTopology

from mininet import node as mininet_node
from mininet import net as mininet_net
from mininet import link as mininet_link
from mininet import cli as mininet_cli
from mininet import util as mininet_util
from forch import faucetizer
from forch.proto.forch_configuration_pb2 import OrchestrationConfig

LOGGER = logger.get_logger('network')


# pylint: disable=too-few-public-methods
class DAQHost(mininet_node.Host):
    """Base Mininet Host class, for Mininet-based tests."""


class DummyNode:
    """Dummy node used to handle shadow devices"""
    # pylint: disable=invalid-name
    def addIntf(self, node, port=None):
        """No-op for adding an interface"""

    def cmd(self, cmd, *args, **kwargs):
        """No-op for running a command"""


class TestNetwork:
    """Test network manager"""

    OVS_CLS = mininet_node.OVSSwitch
    MAX_INTERNAL_DPID = 100
    DEFAULT_OF_PORT = 6653
    DEFAULT_MININET_SUBNET = "10.20.0.0/16"
    _CTRL_PRI_IFACE = 'ctrl-pri'
    INTERMEDIATE_FAUCET_FILE = "inst/faucet_intermediate.yaml"
    OUTPUT_FAUCET_FILE = "inst/faucet.yaml"
    _VXLAN_DEFAULT_PORT = 4789
    _VXLAN_CONFIG_FMT = '%s type=vxlan options:remote_ip=%s options:key=%s'

    def __init__(self, config):
        self.config = config
        self.net = None
        self.pri = None
        self.sec = None
        self.sec_dpid = None
        self.sec_port = None
        self.tap_intf = None
        self._settle_sec = int(config['settle_sec'])
        subnet = config.get('internal_subnet', {}).get('subnet', self.DEFAULT_MININET_SUBNET)
        self._mininet_subnet = ip_network(subnet)
        self.topology = FaucetTopology(self.config)
        self.ext_intf = self.topology.get_ext_intf()
        switch_setup = config.get('switch_setup', {})
        self.ext_ofpt = int(switch_setup.get('lo_port', self.DEFAULT_OF_PORT))
        self.ext_loip = switch_setup.get('mods_addr')
        self.switch_links = {}
        orch_config = OrchestrationConfig()
        self.faucitizer = faucetizer.Faucetizer(
            orch_config, self.INTERMEDIATE_FAUCET_FILE, self.OUTPUT_FAUCET_FILE)

    # pylint: disable=too-many-arguments
    def add_host(self, name, cls=DAQHost, ip_addr=None, env_vars=None, vol_maps=None,
                 port=None, tmpdir=None):
        """Add a host to the ecosystem"""
        params = {'ip': ip_addr} if ip_addr else {}
        params['tmpdir'] = os.path.join(tmpdir, 'nodes') if tmpdir else None
        params['env_vars'] = env_vars if env_vars else []
        params['vol_maps'] = vol_maps if vol_maps else []
        host = self.net.addHost(name, cls, **params)
        try:
            LOGGER.debug('Created host %s with pid %s/%s', name, host.pid, host.shell.pid)
            switch_link = self.net.addLink(self.pri, host, port1=port, fast=False)
            self.switch_links[host] = switch_link
            if self.net.built:
                host.configDefault()
                self._switch_attach(self.pri, switch_link.intf1)
        except Exception as e:
            host.terminate()
            raise e
        return host

    def get_host_interface(self, host):
        """Get the internal link interface for this host"""
        return self.switch_links[host].intf2

    def _switch_attach(self, switch, intf):
        switch.attach(intf)
        # This really should be done in attach, but currently only automatic on switch startup.
        switch.vsctl(switch.intfOpts(intf))

    def _switch_del_intf(self, switch, intf):
        del switch.intfs[switch.ports[intf]]
        del switch.ports[intf]
        del switch.nameToIntf[intf.name]

    def remove_host(self, host):
        """Remove a host from the ecosystem"""
        index = self.net.hosts.index(host)
        if index:
            del self.net.hosts[index]
        if host in self.switch_links:
            switch_link = self.switch_links[host]
            del self.switch_links[host]
            intf = switch_link.intf1
            self.pri.detach(intf)
            self._switch_del_intf(self.pri, intf)
            intf.delete()
            del self.net.links[self.net.links.index(switch_link)]

    def get_subnet(self):
        """Gets the internal mininet subnet"""
        return copy.copy(self._mininet_subnet)

    def _create_secondary(self):
        self.sec_dpid = self.topology.get_sec_dpid()
        self.sec_port = self.topology.get_sec_port()
        if self.ext_intf:
            LOGGER.info('Configuring external secondary with dpid %s on intf %s',
                        self.sec_dpid, self.ext_intf)
            sec_intf = mininet_link.Intf(self.ext_intf, node=DummyNode(), port=1)
            self.pri.addIntf(sec_intf, port=1)
        else:
            LOGGER.info('Creating ovs sec with dpid/port %s/%d', self.sec_dpid, self.sec_port)
            self.sec = self.net.addSwitch('sec', dpid=str(self.sec_dpid), cls=self.OVS_CLS)

            link = self.net.addLink(self.pri, self.sec, port1=1,
                                    port2=self.sec_port, fast=False)
            LOGGER.info('Added switch link %s <-> %s', link.intf1.name, link.intf2.name)
            self.ext_intf = link.intf1.name
            self._attach_sec_device_links()

    def _is_dpid_external(self, dpid):
        return int(dpid) > self.MAX_INTERNAL_DPID

    def _attach_sec_device_links(self):
        topology_intfs = self.topology.get_device_intfs()
        for intf_port in range(1, len(topology_intfs) + 1):
            intf_name = topology_intfs[intf_port-1]
            LOGGER.info("Attaching device interface %s on port %d.", intf_name, intf_port)
            intf = mininet_link.Intf(intf_name, node=DummyNode(), port=intf_port)
            intf.port = intf_port
            self.sec.addIntf(intf, port=intf_port)
            self._switch_attach(self.sec, intf)

    def is_system_port(self, dpid, port):
        """Check if the dpid/port combo is the system trunk port"""
        return dpid == self.topology.PRI_DPID and port == self.topology.PRI_TRUNK_PORT

    def is_device_port(self, dpid, port):
        """Check if the dpid/port combo is for a valid device"""
        target_dpid = int(self.sec_dpid)
        return dpid == target_dpid and port < self.sec_port

    def cli(self):
        """Drop into the mininet CLI"""
        mininet_cli.CLI(self.net)

    def stop(self):
        """Stop network"""
        self.topology.stop()
        self.net.stop()

    def initialize(self):
        """Initialize network"""

        LOGGER.debug("Creating miniet...")
        self.net = mininet_net.Mininet(ipBase=str(self._mininet_subnet))

        LOGGER.debug("Adding primary...")
        self.pri = self.net.addSwitch('pri', dpid='1', cls=self.OVS_CLS)

        LOGGER.info("Activating faucet topology...")
        self.topology.initialize(self.pri)
        self.topology.start()

        LOGGER.info("Initializing faucitizer...")
        self._generate_behavioral_config()

        target_ip = "127.0.0.1"
        LOGGER.debug("Adding controller at %s", target_ip)
        controller = mininet_node.RemoteController
        self.net.addController('controller', controller=controller,
                               ip=target_ip, port=self.ext_ofpt)

        LOGGER.debug("Adding secondary...")
        self._create_secondary()

        LOGGER.info("Starting mininet...")
        self.net.start()

        if self.ext_loip:
            self._attach_switch_interface(self._CTRL_PRI_IFACE)

        self.tap_intf = self._configure_tap_intf()

    def _configure_tap_intf(self):
        vxlan_config = self.config.get('switch_setup', {}).get('vxlan', {})
        if 'key' not in vxlan_config:
            return self.ext_intf

        key = vxlan_config['key']
        remote_ip = vxlan_config['remote_ip']
        port = self._VXLAN_DEFAULT_PORT
        ovs_config = self._VXLAN_CONFIG_FMT % (self.ext_intf, remote_ip, key)
        LOGGER.info('Configuring interface %s', ovs_config)
        if self._settle_sec:
            time.sleep(self._settle_sec)
        self.pri.vsctl('set interface %s' % ovs_config)

        # Use the synthetic vxlan interface, since ext_intf is only virtual (can't run tcpdump).
        return 'vxlan_sys_%d' % port

    def direct_port_traffic(self, target_mac, port, target):
        """Direct traffic for a given mac to target port"""
        dest = target['port_set'] if target else None
        LOGGER.info('Directing traffic for %s on port %s to %s', target_mac, port, dest)
        # TODO: Convert this to use faucitizer to change vlan
        self.topology.direct_port_traffic(target_mac, port, target)
        self._generate_behavioral_config()

    def _generate_behavioral_config(self):
        with open(self.INTERMEDIATE_FAUCET_FILE, 'w') as file:
            network_topology = self.topology.get_network_topology()
            yaml.safe_dump(network_topology, file)

        self.faucitizer.reload_structural_config(self.INTERMEDIATE_FAUCET_FILE)
        if self._settle_sec:
            LOGGER.info('Waiting %ds for network to settle', self._settle_sec)
            time.sleep(self._settle_sec)

    def direct_device_traffic(self, device, port_set):
        """Modify gateway set's vlan to match triggering vlan"""
        LOGGER.info('Directing traffic for %s on vlan %s to %s',
                    device.mac, device.vlan, port_set)
        # TODO: Convert this to use faucitizer to change vlan
        self.topology.direct_device_traffic(device, port_set)
        self._generate_behavioral_config()

    def _attach_switch_interface(self, switch_intf_name):
        switch_port = self.topology.switch_port()
        LOGGER.info('Attaching switch interface %s on port %s', switch_intf_name, switch_port)
        self.pri.vsctl('add-port', self.pri.name, switch_intf_name, '--',
                       'set', 'interface', switch_intf_name, 'ofport_request=%s' % switch_port)

    def delete_mirror_interface(self, port):
        """Delete a mirroring interface on the given port"""
        return self.create_mirror_interface(port, delete=True)

    def create_mirror_interface(self, port, delete=False):
        """Create/delete a mirror interface for the given port"""
        mirror_intf_name = self.topology.mirror_iface_name(port)
        mirror_intf_peer = mirror_intf_name + '-ext'
        mirror_port = self.topology.mirror_port(port)
        if delete:
            LOGGER.info('Deleting mirror pair %s <-> %s', mirror_intf_name, mirror_intf_peer)
            self.pri.cmd('ip link del %s' % mirror_intf_name)
        else:
            LOGGER.info('Creating mirror pair %s <-> %s at %d',
                        mirror_intf_name, mirror_intf_peer, mirror_port)
            mininet_util.makeIntfPair(mirror_intf_name, mirror_intf_peer)
            self.pri.cmd('ip link set %s up' % mirror_intf_name)
            self.pri.cmd('ip link set %s up' % mirror_intf_peer)
            self.pri.vsctl('add-port', self.pri.name, mirror_intf_name, '--',
                           'set', 'interface', mirror_intf_name, 'ofport_request=%s' % mirror_port)
        return mirror_intf_name

    def device_group_for(self, device):
        """Find the target device group for the given device."""
        return self.topology.device_group_for(device)

    def device_group_size(self, group_name):
        """Return the size of the given group."""
        return self.topology.device_group_size(group_name)
