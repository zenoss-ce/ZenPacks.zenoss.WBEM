##############################################################################
#
# Copyright (C) Zenoss, Inc. 2017, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

import logging
log = logging.getLogger('zen.WBEM')

from twisted.internet import ssl, reactor, defer
from twisted.internet.error import TimeoutError
from twisted.python import failure

from zope.component import adapts
from zope.interface import implements

from Products.ZenEvents import ZenEventClasses
from Products.ZenUtils.Utils import prepId
from Products.Zuul.form import schema
from Products.Zuul.infos import ProxyProperty
from Products.Zuul.infos.template import RRDDataSourceInfo
from Products.Zuul.interfaces import IRRDDataSourceInfo
from Products.Zuul.utils import ZuulMessageFactory as _t

from ZenPacks.zenoss.PythonCollector.datasources.PythonDataSource \
    import PythonDataSource, PythonDataSourcePlugin

from ZenPacks.zenoss.WBEM.utils import (
    addLocalLibPath, result_errmsg, create_connection, convert_to_timestamp
)

addLocalLibPath()

from pywbem import CIMDateTime
from pywbem.twisted_client import ExecQuery


def string_to_lines(string):
    if isinstance(string, (list, tuple)):
        return string
    elif hasattr(string, 'splitlines'):
        return string.splitlines()

    return None


class WBEMDataSource(PythonDataSource):
    """Datasource used to capture datapoints from WBEM providers."""

    ZENPACKID = 'ZenPacks.zenoss.WBEM'

    sourcetypes = ('WBEM',)
    sourcetype = sourcetypes[0]

    plugin_classname = 'ZenPacks.zenoss.WBEM.datasources.WBEMDataSource.WBEMDataSourcePlugin'

    namespace = ''
    query_language = 'CQL'  # hard-coded for now.
    query = ''
    result_component_key = ''
    result_component_value = ''
    result_timestamp_key = ''

    _properties = PythonDataSource._properties + (
        {'id': 'namespace', 'type': 'string'},
        {'id': 'query_language', 'type': 'string'},
        {'id': 'query', 'type': 'lines'},
        {'id': 'result_component_key', 'type': 'string'},
        {'id': 'result_component_value', 'type': 'string'},
        {'id': 'result_timestamp_key', 'type': 'string'},
        )


class IWBEMDataSourceInfo(IRRDDataSourceInfo):
    cycletime = schema.TextLine(
        title=_t(u'Cycle Time (seconds)'))

    namespace = schema.TextLine(
        group=_t('WBEM'),
        title=_t('Namespace'))

    query = schema.Text(
        group=_t(u'WBEM'),
        title=_t('CQL Query'),
        xtype='twocolumntextarea')

    result_component_key = schema.TextLine(
        group=_t(u'WBEM Results'),
        title=_t(u'Result Component Key'))

    result_component_value = schema.TextLine(
        group=_t(u'WBEM Results'),
        title=_t(u'Result Component Value'))

    result_timestamp_key = schema.TextLine(
        group=_t(u'WBEM Results'),
        title=_t(u'Result Timestamp Key'))


class WBEMDataSourceInfo(RRDDataSourceInfo):
    implements(IWBEMDataSourceInfo)
    adapts(WBEMDataSource)

    testable = False

    cycletime = ProxyProperty('cycletime')

    namespace = ProxyProperty('namespace')
    result_component_key = ProxyProperty('result_component_key')
    result_component_value = ProxyProperty('result_component_value')
    result_timestamp_key = ProxyProperty('result_timestamp_key')

    @property
    def query(self):
        return "\n".join(self._object.query)

    @query.setter
    def query(self, val):
        self._object.query = string_to_lines(val)


class WBEMDataSourcePlugin(PythonDataSourcePlugin):
    proxy_attributes = (
        'zWBEMPort', 'zWBEMUsername', 'zWBEMPassword', 'zWBEMUseSSL', 'zWBEMRequestTimeout'
        )

    @classmethod
    def config_key(cls, datasource, context):
        params = cls.params(datasource, context)
        return (
            context.device().id,
            datasource.getCycleTime(context),
            datasource.rrdTemplate().id,
            datasource.id,
            datasource.plugin_classname,
            params.get('namespace'),
            params.get('query_language'),
            params.get('query'),
            )

    @classmethod
    def params(cls, datasource, context):
        params = {}

        params['namespace'] = datasource.talesEval(
            datasource.namespace, context)

        params['query_language'] = datasource.query_language
        params['query'] = datasource.talesEval(
            ' '.join(string_to_lines(datasource.query)), context)

        params['result_component_key'] = datasource.talesEval(
            datasource.result_component_key, context)

        params['result_component_value'] = datasource.talesEval(
            datasource.result_component_value, context)

        params['result_timestamp_key'] = datasource.talesEval(
            datasource.result_timestamp_key, context)

        return params

    def collect(self, config):

        ds0 = config.datasources[0]

        credentials = (ds0.zWBEMUsername, ds0.zWBEMPassword)

        factory = ExecQuery(
            credentials,
            ds0.params['query_language'],
            ds0.params['query'],
            namespace=ds0.params['namespace'])

        create_connection(ds0, factory)

        return add_timeout(factory, int(ds0.zWBEMRequestTimeout))

    def onSuccess(self, results, config):
        data = self.new_data()
        ds0 = config.datasources[0]

        if not isinstance(results, list):
            results = [results]

        ds0 = config.datasources[0]
        log.debug('Monitoring template name: {}'.format(ds0.template))
        log.debug('Monitoring query: {}'.format(ds0.params['query']))
        for instance in results:
            try:
                log.debug('Monitoring result: {0}'.format(instance.__dict__))
            except AttributeError:
                log.debug('Monitoring result is empty')

        # Convert datasources to a dictionary with result_component_value as
        # the key. This allows us to avoid an inner loop below.
        datasources = dict(
            (x.params.get('result_component_value', ''), x) \
                for x in config.datasources)

        result_component_key = \
            ds0.params['result_component_key']

        for result in results:
            if result_component_key:
                datasource = datasources.get(result[result_component_key])

                if not datasource:
                    log.debug("No datasource for result: %r", result.items())
                    continue

            else:
                datasource = ds0

            if result_component_key and result_component_key in result:
                result_component_value = datasource.params.get(
                    'result_component_value')

                if result_component_value != result[result_component_key]:
                    continue

            component_id = prepId(datasource.component)

            # Determine the timestamp that the value was collected.
            result_timestamp_key = datasource.params.get(
                'result_timestamp_key')

            timestamp = None

            if result_timestamp_key and result_timestamp_key in result:
                cim_date = result[result_timestamp_key]
                timestamp = convert_to_timestamp(cim_date)

            if not timestamp:
                timestamp = 'N'

            for datapoint in datasource.points:
                if datapoint.id in result:
                    value = result[datapoint.id]
                    if isinstance(value, CIMDateTime):
                        value = convert_to_timestamp(value)
                    data['values'][component_id][datapoint.id] = \
                        (value, timestamp)

        data['events'].append({
            'eventClassKey': 'wbemCollectionSuccess',
            'eventKey': 'wbemCollection',
            'summary': 'WBEM: successful collection',
            'device': config.id,
            'eventClass': ds0.eventClass,
            'severity': ZenEventClasses.Clear
        })

        return data

    def onError(self, result, config):
        errmsg = 'WBEM: %s' % result_errmsg(result)
        ds0 = config.datasources[0]

        if isinstance(result, failure.Failure) and isinstance(result.value, defer.CancelledError):
            errmsg = 'WBEM: %s' % 'request time exceeds value of zWBEMRequestTimeout'

        log.error('%s %s', config.id, errmsg)

        data = self.new_data()
        data['events'].append({
            'eventClassKey': 'wbemCollectionError',
            'eventKey': 'wbemCollection',
            'summary': errmsg,
            'device': config.id,
            'severity': ds0.severity,
            'eventClass': ds0.eventClass
        })

        return data


def add_timeout(factory, seconds):
    """Return new Deferred that will errback TimeoutError after seconds."""
    deferred_with_timeout = defer.Deferred()
    deferred = factory.deferred

    def fire_timeout():
        deferred.cancel()
        if not deferred_with_timeout.called:
            deferred_with_timeout.errback(failure.Failure(TimeoutError()))

    delayed_timeout = reactor.callLater(seconds, fire_timeout)
    factory.deferred_timeout = delayed_timeout

    def handle_result(result):
        if delayed_timeout.active():
            delayed_timeout.cancel()

        if not deferred_with_timeout.called:
            if isinstance(result, failure.Failure):
                deferred_with_timeout.errback(result)
            else:
                deferred_with_timeout.callback(result)

    deferred.addBoth(handle_result)
    return deferred_with_timeout
