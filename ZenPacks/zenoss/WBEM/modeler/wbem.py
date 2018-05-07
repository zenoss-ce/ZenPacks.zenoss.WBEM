##############################################################################
#
# Copyright (C) Zenoss, Inc. 2012, 2016, 2017, 2018 all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

"""API interface to the PyWBEM library.

WBEM classes available
    EnumerateClassNames
    EnumerateClasses
    EnumerateInstances
    EnumerateInstanceNames

example:
    wbemQueries = {
        'ec':'root/emc',
        'ein':'root/emc:CIM_ManagedElement'
        }

You must also have the zWBEMPort, zWBEMUsername and zWBEMPassword properties
set to succesfully pull data.

"""
import itertools

from pywbem.utils import extend_results

from twisted.internet import ssl, reactor
from twisted.internet.defer import DeferredList, CancelledError

from Products.DataCollector.plugins.CollectorPlugin import PythonPlugin

from ZenPacks.zenoss.WBEM.utils import (
    addLocalLibPath,
    result_errmsg,
)

from ZenPacks.zenoss.WBEM.patches import (
    EnumerateClassNames,
    EnumerateClasses,
    EnumerateInstanceNames,
    EnumerateInstances,
    OpenEnumerateInstances,
    PullInstances,
)

addLocalLibPath()

DEFAULT_CIM_NAMESPACE = 'root/cimv2'


def get_enumerate_instances(creds, classname, host, port, ssl,
                            namespace=DEFAULT_CIM_NAMESPACE, **kwargs):
    """Choose appropriate method based on kwargs and return the instance which implements it."""

    if kwargs.get('MaxObjectCount', 0) > 0 or kwargs.get('OperationTimeout', 0) > 0:
        klass = OpenEnumerateInstances
    else:
        klass = EnumerateInstances

        for unsupported_kwarg in ['MaxObjectCount', 'OperationTimeout']:
            kwargs.pop(unsupported_kwarg, None)

    return klass(creds, classname, host, port, ssl, namespace, **kwargs)


class WBEMPlugin(PythonPlugin):
    deviceProperties = PythonPlugin.deviceProperties + (
        'zWBEMPort',
        'zWBEMUsername',
        'zWBEMPassword',
        'zWBEMUseSSL',
        'zWBEMMaxObjectCount',
        'zWBEMOperationTimeout',
    )

    wbemQueries = {}

    def collect(self, device, log):
        if not device.manageIp:
            log.error('%s has no management IP address', device.id)

        if not device.zWBEMPort:
            log.error("zWBEMPort empty for %s", device.id)

        if not device.zWBEMUsername:
            log.error("zWBEMUsername empty for %s", device.id)

        if not device.zWBEMPassword:
            log.error("zWBEMPassword empty for %s", device.id)

        if not device.manageIp or \
                not device.zWBEMPort or \
                not device.zWBEMUsername or \
                not device.zWBEMPassword:
            return None

        deferreds = []

        for wbemnamespace, wbemclass in self.wbemQueries.items():
            namespaces = wbemnamespace.split(":")
            namespace = namespaces[0]
            if len(namespaces) > 1:
                classname = namespaces[1]

            userCreds = (device.zWBEMUsername, device.zWBEMPassword)

            if wbemclass == 'ec':
                wbemClass = EnumerateClasses(
                    userCreds, host=device.manageIp, port=device.zWBEMPort,
                    ssl=device.zWBEMUseSSL, namespace=namespace)

            elif wbemclass == 'ecn':
                wbemClass = EnumerateClassNames(
                    userCreds, host=device.manageIp, port=device.zWBEMPort,
                    ssl=device.zWBEMUseSSL, namespace=namespace)

            elif wbemclass == 'ei':
                wbemClass = get_enumerate_instances(
                    userCreds, namespace=namespace,
                    host=device.manageIp, port=device.zWBEMPort,
                    ssl=device.zWBEMUseSSL,
                    classname=classname,
                    MaxObjectCount=device.zWBEMMaxObjectCount,
                    OperationTimeout=device.zWBEMOperationTimeout)

            elif wbemclass == 'ein':
                wbemClass = EnumerateInstanceNames(
                    userCreds, namespace=namespace, host=device.manageIp, port=device.zWBEMPort,
                    ssl=device.zWBEMUseSSL, classname=classname)

            else:
                log.warn('Incorrect class call %s', wbemclass)
                wbemClass = EnumerateClasses(userCreds,
                                             host=device.manageIp, port=device.zWBEMPort,
                                             ssl=device.zWBEMUseSSL,
                                             namespace=namespace)

            wbemClass.deferred.addCallback(check_if_complete,
                                           device, namespace, classname)
            deferreds.append(wbemClass.deferred)

        # Execute the deferreds and return the results to the callback.
        d = DeferredList(deferreds, consumeErrors=True)
        add_collector_timeout(
            d, device.zCollectorClientTimeout
        )
        d.addCallback(self.check_results, device, log)

        return d

    def check_results(self, results, device, log):
        """Check results for errors."""

        # If all results are failures we have a problem to report.
        if len(results) and True not in set(x[0] for x in results):
            log.error('%s WBEM: %s', device.id, result_errmsg(results[0][1]))

            # This will allow for an event to be created by the device class.
            results = "ERROR", result_errmsg(results[0][1])

            return results

        try:
            results_new = []
            for success, instances in results:
                if success:
                    inst = []
                    for instance in instances:
                        inst.append(instance.__dict__)
                    results_new.append((success, inst))
                else:
                    results_new.append((success, instances))

            log.debug('Results: {0}'.format(results_new))
        except:
            pass

        return results


def add_collector_timeout(deferred, seconds):
    """Raise error on deferred when modeler is timed out."""
    error = CancelledError("WBEM query timeout")

    def handle_timeout():
        deferred.cancel()

    def handle_result(result):
        if timeout_d.active():
            timeout_d.cancel()
        if isinstance(result, list):
            for item in result:
                if not item[0] and item[1].check(CancelledError):
                    raise error
        return result

    def handle_failure(failure):
        # define this method to catch errors from canceled deferredlist
        # on Zenoss 4.2.x with Twisted v.11
        if failure.check(CancelledError):
            raise error

        return failure

    timeout_d = reactor.callLater(seconds, handle_timeout)
    deferred.addBoth(handle_result)
    deferred.addErrback(handle_failure)

    return deferred


def check_if_complete(results, device, namespace, classname,
                      results_aggregator=None, **kwargs):
    if not results_aggregator:
        results_aggregator = []

    if isinstance(results, tuple):
        query_results = results[0]
        enumeration_context = results[2]

        results_aggregator = extend_aggregated_results(query_results,
                                                       results_aggregator)

        credentials = (device.zWBEMUsername, device.zWBEMPassword)

        wbemClass = PullInstances(
            credentials,
            namespace,
            device.manageIp,
            device.zWBEMPort,
            device.zWBEMUseSSL,
            enumeration_context,
            device.zWBEMMaxObjectCount,
            classname,
            **kwargs
        )
        wbemClass.deferred.addCallback(
            check_if_complete,
            device,
            namespace,
            classname,
            results_aggregator=results_aggregator,
            **kwargs
        )
        return wbemClass.deferred

    results_aggregator = extend_aggregated_results(results,
                                                   results_aggregator)

    if isinstance(results_aggregator, list) and \
            isinstance(results_aggregator[0], list):
        results_aggregator = list(
            itertools.chain.from_iterable(results_aggregator)
        )

    return results_aggregator


def extend_aggregated_results(results, results_aggregator):
    if isinstance(results, dict):
        if not results_aggregator:
            results_aggregator = {}
        extend_results(results_aggregator, results)
    else:
        results_aggregator.append(results)
    return results_aggregator
