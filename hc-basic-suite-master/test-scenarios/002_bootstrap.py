# -*- coding: utf-8 -*-
#
# Copyright 2014, 2017 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import

import functools
import os
import random
import time
import threading

import nose.tools as nt
from nose.tools import assert_false
from nose import SkipTest

from ovirtsdk.infrastructure import errors
from ovirtsdk.xml import params

# TODO: import individual SDKv4 types directly (but don't forget sdk4.Error)
import ovirtsdk4 as sdk4
import ovirtsdk4.types as types

from lago import utils
from ovirtlago import testlib

import test_utils
from test_utils import network_utils_v4
from test_utils import constants
from test_utils import versioning

import logging
LOGGER = logging.getLogger(__name__)

# DC/Cluster
DC_NAME = 'Default'
DC_VER_MAJ = 4
DC_VER_MIN = 2
SD_FORMAT = 'v4'
CLUSTER_NAME = 'Default'
DC_QUOTA_NAME = 'DC-QUOTA'

# Storage
MASTER_SD_TYPE = 'glusterfs'
MASTER_SD_GLUSTER_VOL = 'vmstore'
MASTER_SD_NAME = 'vmstore'

SD_NFS_NAME = 'nfs'
SD_NFS_HOST_NAME = testlib.get_prefixed_name('storage')
SD_NFS_PATH = '/exports/nfs/share1'

SD_ISCSI_NAME = 'iscsi'
SD_ISCSI_HOST_NAME = testlib.get_prefixed_name('engine')
SD_ISCSI_TARGET = 'iqn.2014-07.org.ovirt:storage'
SD_ISCSI_PORT = 3260
SD_ISCSI_NR_LUNS = 2

SD_ISO_NAME = 'iso'
SD_ISO_HOST_NAME = SD_NFS_HOST_NAME
SD_ISO_PATH = '/exports/nfs/iso'

SD_TEMPLATES_NAME = 'templates'
SD_TEMPLATES_HOST_NAME = SD_NFS_HOST_NAME
SD_TEMPLATES_PATH = '/exports/nfs/exported'

SD_GLANCE_NAME = 'ovirt-image-repository'
GLANCE_AVAIL = False
GLANCE_SERVER_URL = 'http://glance.ovirt.org:9292/'

CIRROS_IMAGE_NAME = 'CirrOS 0.4.0 for x86_64'

# Network
VLAN200_NET = 'VLAN200_Network'
VLAN100_NET = 'VLAN100_Network'

VM_NETWORK = u'VM Network with a very long name and עברית'
VM_NETWORK_VLAN_ID = 100
MIGRATION_NETWORK = 'Migration_Net'

def _get_host_ips_in_net(prefix, host_name, net_name):
    return prefix.virt_env.get_vm(host_name).ips_in_net(net_name)

def _get_host_ip(prefix, host_name):
    vm = prefix.virt_env.get_vm(host_name)
    return prefix.virt_env.get_vm(host_name).ip()

def _hosts_in_dc(api, dc_name=DC_NAME):
    hosts = api.hosts.list(query='datacenter={}'.format(dc_name))
    return sorted(hosts, key=lambda host: host.name)

def _random_host_from_dc(api, dc_name=DC_NAME):
    return random.choice(_hosts_in_dc(api, dc_name))


def remove_default_cluster(api):
    nt.assert_true(api.clusters.get(name='Default').delete())


@testlib.with_ovirt_prefix
def wait_engine(prefix):

    def _engine_is_up():
        engine = prefix.virt_env.engine_vm()
        try:
            if engine and engine.get_api_v4():
                return True
        except:
            return

    testlib.assert_true_within(_engine_is_up, timeout=35 * 60)

@testlib.with_ovirt_api4
def add_dc_quota(api):
    datacenters_service = api.system_service().data_centers_service()
    datacenter = datacenters_service.list(search='name=%s' % DC_NAME)[0]
    datacenter_service = datacenters_service.data_center_service(datacenter.id)
    quotas_service = datacenter_service.quotas_service()
    nt.assert_true(
        quotas_service.add(
            types.Quota (
                name=DC_QUOTA_NAME,
                description='DC-QUOTA-DESCRIPTION',
                data_center=datacenter,
                cluster_soft_limit_pct=99
            )
        )
    )


@testlib.with_ovirt_prefix
def install_cockpit_ovirt(prefix):
    def _install_cockpit_ovirt_on_host(host):
        ret = host.ssh(['yum', '-y', 'install', 'cockpit-ovirt-dashboard'])
        nt.assert_equals(ret.code, 0, '_install_cockpit_ovirt_on_host(): failed to install cockpit-ovirt-dashboard on host %s' % host)
        return True

    hosts = prefix.virt_env.host_vms()
    vec = utils.func_vector(_install_cockpit_ovirt_on_host, [(h,) for h in hosts])
    vt = utils.VectorThread(vec)
    vt.start_all()
    nt.assert_true(all(vt.join_all()), 'not all threads finished: %s' % vt)


def _add_storage_domain(api, p):
    dc = api.datacenters.get(DC_NAME)
    sd = api.storagedomains.add(p)
    nt.assert_true(sd)
    nt.assert_true(
        api.datacenters.get(
            DC_NAME,
        ).storagedomains.add(
            api.storagedomains.get(
                sd.name,
            ),
        )
    )

    if dc.storagedomains.get(sd.name).status.state == 'maintenance':
        sd.activate()

    testlib.assert_true_within_long(
        lambda: dc.storagedomains.get(sd.name).status.state == 'active'
    )

@testlib.with_ovirt_prefix
def add_master_storage_domain(prefix):
    if MASTER_SD_TYPE == 'glusterfs':
        add_glusterfs_storage_domain(prefix, MASTER_SD_NAME, MASTER_SD_GLUSTER_VOL)
    else:
        add_nfs_storage_domain(prefix)

def add_datastore_storage_domain(prefix):
    add_glusterfs_storage_domain(prefix, "data", "data")

def add_nfs_storage_domain(prefix):
    add_generic_nfs_storage_domain(prefix, SD_NFS_NAME, SD_NFS_HOST_NAME, SD_NFS_PATH)

def add_generic_nfs_storage_domain(prefix, sd_nfs_name, nfs_host_name, mount_path, sd_format=SD_FORMAT, sd_type='data', nfs_version='v4_1'):
    if sd_type == 'data':
        dom_type = sdk4.types.StorageDomainType.DATA
    elif sd_type == 'iso':
        dom_type = sdk4.types.StorageDomainType.ISO
    elif sd_type == 'export':
        dom_type = sdk4.types.StorageDomainType.EXPORT

    if nfs_version == 'v3':
        nfs_vers = sdk4.types.NfsVersion.V3
    elif nfs_version == 'v4':
        nfs_vers = sdk4.types.NfsVersion.V4
    elif nfs_version == 'v4_1':
        nfs_vers = sdk4.types.NfsVersion.V4_1
    elif nfs_version == 'v4_2':
        nfs_vers = sdk4.types.NfsVersion.V4_2
    else:
        nfs_vers = sdk4.types.NfsVersion.AUTO

    api = prefix.virt_env.engine_vm().get_api(api_ver=4)
    ips = _get_host_ips_in_net(prefix, nfs_host_name, testlib.get_prefixed_name('net-storage'))
    kwargs = {}
    if sd_format >= 'v4':
        if not versioning.cluster_version_ok(4, 1):
            kwargs['storage_format'] = sdk4.types.StorageFormat.V3
        elif not versioning.cluster_version_ok(4, 3):
            kwargs['storage_format'] = sdk4.types.StorageFormat.V4
    random_host = _random_host_from_dc(api, DC_NAME)
    LOGGER.debug('random host: {}'.format(random_host.name))

    p = sdk4.types.StorageDomain(
        name=sd_nfs_name,
        description='APIv4 NFS storage domain',
        type=dom_type,
        host=random_host,
        storage=sdk4.types.HostStorage(
            type=sdk4.types.StorageType.NFS,
            address=ips[0],
            path=mount_path,
            nfs_version=nfs_vers,
        ),
        **kwargs
    )
    _add_storage_domain(api, p)

def add_glusterfs_storage_domain(prefix, sdname, volname):
    api = prefix.virt_env.engine_vm().get_api()
    hosts = sorted([vm.name() for vm in prefix.virt_env.host_vms()])
    mount_path = "{0}://{1}".format(hosts[0], volname)
    mount_options = "backup-volfile-servers={0}".format(':'.join(hosts[1:]))

    p = params.StorageDomain(
        name=sdname,
        data_center=params.DataCenter(
            name=DC_NAME,
        ),
        type_='data',
        storage_format='v3',
        host=_random_host_from_dc(api, DC_NAME),
        storage=params.Storage(
            type_='glusterfs',
            path=mount_path,
            vfs_type='glusterfs',
            mount_options=mount_options,
        ),
    )
    _add_storage_domain(api, p)

@testlib.with_ovirt_prefix
def wait_hosts(prefix):
    api = prefix.virt_env.engine_vm().get_api_v4()
    hosts_service = api.system_service().hosts_service()

    def _host_is_up():
        host_service = hosts_service.host_service(api_host.id)
        host_obj = host_service.get()
        if host_obj.status == sdk4.types.HostStatus.UP:
            return True

        if host_obj.status == sdk4.types.HostStatus.NON_OPERATIONAL:
            raise RuntimeError('Host %s is in non operational state' % api_host.name)
        if host_obj.status == sdk4.types.HostStatus.INSTALL_FAILED:
            raise RuntimeError('Host %s installation failed' % api_host.name)
        if host_obj.status == sdk4.types.HostStatus.NON_RESPONSIVE:
            raise RuntimeError('Host %s is in non responsive state' % api_host.name)

    api_hosts = hosts_service.list()
    nt.assert_equals(len(api_hosts), 3)
    for api_host in api_hosts:
        testlib.assert_true_within(_host_is_up, timeout=35*60)


@testlib.with_ovirt_prefix
def add_secondary_storage_domains(prefix):
    vt = utils.VectorThread(
            [
                functools.partial(import_non_template_from_glance, prefix),
                functools.partial(import_template_from_glance, prefix),
                functools.partial(add_datastore_storage_domain, prefix),
            ],
        )
    vt.start_all()
    vt.join_all()


def add_iscsi_storage_domain(prefix):
    api = prefix.virt_env.engine_vm().get_api()

    # Find LUN GUIDs
    ret = prefix.virt_env.get_vm(SD_ISCSI_HOST_NAME).ssh(['cat', '/root/multipath.txt'])
    nt.assert_equals(ret.code, 0)

    lun_guids = ret.out.splitlines()[:SD_ISCSI_NR_LUNS]

    p = params.StorageDomain(
        name=SD_ISCSI_NAME,
        data_center=params.DataCenter(
            name=DC_NAME,
        ),
        type_='data',
        storage_format=SD_FORMAT,
        host=_random_host_from_dc(api, DC_NAME),
        storage=params.Storage(
            type_='iscsi',
            volume_group=params.VolumeGroup(
                logical_unit=[
                    params.LogicalUnit(
                        id=lun_id,
                        address=_get_host_ip(
                            prefix,
                            SD_ISCSI_HOST_NAME,
                        ),
                        port=SD_ISCSI_PORT,
                        target=SD_ISCSI_TARGET,
                        username='username',
                        password='password',
                    ) for lun_id in lun_guids
                ]

            ),
        ),
    )
    _add_storage_domain(api, p)


def add_iso_storage_domain(prefix):
    raise SkipTest('TBD:Change to glusterfs iso domain')
    add_generic_nfs_storage_domain(prefix, SD_ISO_NAME, SD_ISO_HOST_NAME, SD_ISO_PATH, sd_format='v1', sd_type='iso', nfs_version='v3')


def add_templates_storage_domain(prefix):
    raise SkipTest('TBD:Change to glusterfs export domain')
    add_generic_nfs_storage_domain(prefix, SD_TEMPLATES_NAME, SD_TEMPLATES_HOST_NAME, SD_TEMPLATES_PATH, sd_format='v1', sd_type='export')

def generic_import_from_glance(api, image_name=CIRROS_IMAGE_NAME, as_template=False, image_ext='_glance_disk', template_ext='_glance_template', dest_storage_domain=MASTER_SD_NAME, dest_cluster=CLUSTER_NAME):
    glance_provider = api.storagedomains.get(SD_GLANCE_NAME)
    target_image = glance_provider.images.get(name=image_name)
    disk_name = image_name.replace(" ", "_") + image_ext
    template_name = image_name.replace(" ", "_") + template_ext
    import_action = params.Action(
        storage_domain=params.StorageDomain(
            name=dest_storage_domain,
        ),
        cluster=params.Cluster(
            name=dest_cluster,
        ),
        import_as_template=as_template,
        disk=params.Disk(
            name=disk_name,
        ),
        template=params.Template(
            name=template_name,
        ),
    )

    nt.assert_true(
        target_image.import_image(import_action)
    )

    testlib.assert_true_within_long(
        lambda: api.disks.get(disk_name).status.state == 'ok',
    )


def check_glance_connectivity(engine):
    avail = False
    providers_service = engine.openstack_image_providers_service()
    providers = [
        provider for provider in providers_service.list()
        if provider.name == SD_GLANCE_NAME
    ]
    if providers:
        glance = providers_service.provider_service(providers.pop().id)
        try:
            glance.test_connectivity()
            avail = True
        except sdk4.Error:
            pass

    return avail


@testlib.with_ovirt_api4
def list_glance_images(api):
    global GLANCE_AVAIL
    search_query = 'name={}'.format(SD_GLANCE_NAME)
    engine = api.system_service()
    storage_domains_service = engine.storage_domains_service()
    glance_domain_list = storage_domains_service.list(search=search_query)

    if not glance_domain_list:
        openstack_glance = add_glance(api)
        if not openstack_glance:
            raise SkipTest('GLANCE storage domain is not available.')
        glance_domain_list = storage_domains_service.list(search=search_query)

    if not check_glance_connectivity(engine):
        raise SkipTest('GLANCE connectivity test failed')

    glance_domain = glance_domain_list.pop()
    glance_domain_service = storage_domains_service.storage_domain_service(
        glance_domain.id
    )

    try:
        with test_utils.TestEvent(engine, 998):
            all_images = glance_domain_service.images_service().list()
        if len(all_images):
            GLANCE_AVAIL = True
    except sdk4.Error:
        raise SkipTest('GLANCE is not available: client request error')


def add_glance(api):
    target_server = sdk4.types.OpenStackImageProvider(
        name=SD_GLANCE_NAME,
        description=SD_GLANCE_NAME,
        url=GLANCE_SERVER_URL,
        requires_authentication=False
    )

    try:
        providers_service = api.system_service().openstack_image_providers_service()
        providers_service.add(target_server)
        glance = []

        def get():
            providers = [
                provider for provider in providers_service.list()
                if provider.name == SD_GLANCE_NAME
            ]
            if not providers:
                return False
            instance = providers_service.provider_service(providers.pop().id)
            if instance:
                glance.append(instance)
                return True
            else:
                return False

        testlib.assert_true_within_short(func=get, allowed_exceptions=[sdk4.NotFoundError])
    except (AssertionError, sdk4.NotFoundError):
        # RequestError if add method was failed.
        # AssertionError if add method succeed but we couldn't verify that glance was actually added
        return None

    return glance.pop()


def import_non_template_from_glance(prefix):
    api = prefix.virt_env.engine_vm().get_api()
    if not GLANCE_AVAIL:
        raise SkipTest('%s: GLANCE is not available.' % import_non_template_from_glance.__name__ )
    generic_import_from_glance(api)


def import_template_from_glance(prefix):
    api = prefix.virt_env.engine_vm().get_api()
    if not GLANCE_AVAIL:
        raise SkipTest('%s: GLANCE is not available.' % import_template_from_glance.__name__ )
    generic_import_from_glance(api, image_name=CIRROS_IMAGE_NAME, image_ext='_glance_template', as_template=True)

@testlib.with_ovirt_api4
def set_dc_quota_audit(api):
    dcs_service = api.system_service().data_centers_service()
    dc = dcs_service.list(search='name=%s' % DC_NAME)[0]
    dc_service = dcs_service.data_center_service(dc.id)
    nt.assert_true(
        dc_service.update(
            types.DataCenter(
                quota_mode=types.QuotaModeType.AUDIT,
            ),
        )
   )


@testlib.with_ovirt_api4
def add_quota_storage_limits(api):

    # Find the data center and the service that manages it:
    dcs_service = api.system_service().data_centers_service()
    dc = dcs_service.list(search='name=%s' % DC_NAME)[0]
    dc_service = dcs_service.data_center_service(dc.id)

    # Find the storage domain and the service that manages it:
    sds_service = api.system_service().storage_domains_service()
    sd = sds_service.list()[0]

    # Find the quota and the service that manages it.
    # If the quota doesn't exist,create it.
    quotas_service = dc_service.quotas_service()
    quotas = quotas_service.list()

    quota = next(
        (q for q in quotas if q.name == DC_QUOTA_NAME ),
        None
    )
    if quota is None:
        quota = quotas_service.add(
            quota=types.Quota(
                name=DC_QUOTA_NAME,
                description='DC-QUOTA-DESCRIPTION',
                cluster_hard_limit_pct=20,
                cluster_soft_limit_pct=80,
                storage_hard_limit_pct=20,
                storage_soft_limit_pct=80
            )
        )
    quota_service = quotas_service.quota_service(quota.id)

    # Find the quota limit for the storage domain that we are interested on:
    limits_service = quota_service.quota_storage_limits_service()
    limits = limits_service.list()
    limit = next(
        (l for l in limits if l.id == sd.id),
        None
    )

    # If that limit exists we will delete it:
    if limit is not None:
        limit_service = limits_service.limit_service(limit.id)
        limit_service.remove()

    # Create the limit again, with the desired value
    nt.assert_true(
        limits_service.add(
            limit=types.QuotaStorageLimit(
                limit=500,
            )
        )
    )

@testlib.with_ovirt_api4
def add_quota_cluster_limits(api):
    datacenters_service = api.system_service().data_centers_service()
    datacenter = datacenters_service.list(search='name=%s' % DC_NAME)[0]
    datacenter_service = datacenters_service.data_center_service(datacenter.id)
    quotas_service = datacenter_service.quotas_service()
    quotas = quotas_service.list()
    quota = next(
        (q for q in quotas if q.name == DC_QUOTA_NAME),
        None
    )
    quota_service = quotas_service.quota_service(quota.id)
    quota_cluster_limits_service = quota_service.quota_cluster_limits_service()
    nt.assert_true(
        quota_cluster_limits_service.add(
            types.QuotaClusterLimit(
                vcpu_limit=20,
                memory_limit=10000.0
            )
        )
)


@testlib.with_ovirt_api4
def add_vm_network(api):
    engine = api.system_service()

    network = network_utils_v4.create_network_params(
        VM_NETWORK,
        DC_NAME,
        description='VM Network (originally on VLAN {})'.format(
            VM_NETWORK_VLAN_ID),
        vlan=sdk4.types.Vlan(
            id=VM_NETWORK_VLAN_ID,
        ),
    )

    with test_utils.TestEvent(engine, 942): # NETWORK_ADD_NETWORK event
        nt.assert_true(
            engine.networks_service().add(network)
        )

    cluster_service = test_utils.get_cluster_service(engine, CLUSTER_NAME)
    nt.assert_true(
        cluster_service.networks_service().add(network)
    )


@testlib.with_ovirt_api4
def add_non_vm_network(api):
    engine = api.system_service()

    network = network_utils_v4.create_network_params(
        MIGRATION_NETWORK,
        DC_NAME,
        description='Non VM Network on VLAN 200, MTU 9000',
        vlan=sdk4.types.Vlan(
            id='200',
        ),
        usages=[],
        mtu=9000,
    )

    with test_utils.TestEvent(engine, 942): # NETWORK_ADD_NETWORK event
        nt.assert_true(
            engine.networks_service().add(network)
        )

    cluster_service = test_utils.get_cluster_service(engine, CLUSTER_NAME)
    nt.assert_true(
        cluster_service.networks_service().add(network)
    )


@testlib.with_ovirt_prefix
def run_log_collector(prefix):
    engine = prefix.virt_env.engine_vm()
    result = engine.ssh(
        [
            'ovirt-log-collector',
            '--conf-file=/root/ovirt-log-collector.conf',
        ],
    )
    nt.eq_(
        result.code, 0, 'log collector failed. Exit code is %s' % result.code
    )

    engine.ssh(
        [
            'rm',
            '-rf',
            '/dev/shm/sosreport-LogCollector-*',
        ],
    )

@testlib.with_ovirt_prefix
def sleep(prefix):
    time.sleep(120)

_TEST_LIST = [
    wait_engine,
    #wait_hosts,
    list_glance_images,
    add_non_vm_network,
    add_vm_network,
    add_dc_quota,
    add_quota_storage_limits,
    add_quota_cluster_limits,
    set_dc_quota_audit,
]


def test_gen():
    for t in testlib.test_sequence_gen(_TEST_LIST):
        test_gen.__name__ = t.description
        yield t
