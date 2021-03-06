#
# (C) Copyright 2005,2007 Hewlett-Packard Development Company, L.P.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
#

# Author: Tim Potter <tpot@hp.com>

"""pywbem.twisted - WBEM client bindings for Twisted Python.

This module contains factory classes that produce WBEMClient instances
that perform WBEM requests over HTTP using the
twisted.protocols.http.HTTPClient base class.
"""

from twisted.internet import reactor, protocol, defer
from twisted.web import http, client, error

from pywbem import CIMClass, CIMClassName, CIMInstance, CIMInstanceName, CIMError, CIMDateTime, cim_types, cim_xml, cim_obj
from cim_constants import CIM_ERR_INVALID_PARAMETER, DEFAULT_ITER_MAXOBJECTCOUNT

try:
    from elementtree.ElementTree import fromstring, tostring
except ImportError, arg:
    from xml.etree.ElementTree import fromstring, tostring

import six
import string, base64

from types import StringTypes
from datetime import datetime, timedelta

from utils import extend_results


protocol.ClientFactory.noisy = False


class WBEMClient(http.HTTPClient):
    """A HTTPClient subclass that handles WBEM requests."""

    status = None

    def connectionMade(self):
        """Send a HTTP POST command with the appropriate CIM over HTTP
        headers and payload."""

        self.factory.request_xml = str(self.factory.payload)

        self.sendCommand('POST', '/cimom')

        self.sendHeader('Host', '%s:%d' %
                        (self.transport.addr[0], self.transport.addr[1]))
        self.sendHeader('User-Agent', 'pywbem/twisted')
        self.sendHeader('Content-length', len(self.factory.payload))
        self.sendHeader('Content-type', 'application/xml')

        if self.factory.creds:
            auth = base64.b64encode('%s:%s' % (self.factory.creds[0],
                                               self.factory.creds[1]))

            self.sendHeader('Authorization', 'Basic %s' % auth)

        self.sendHeader('CIMOperation', str(self.factory.operation))
        self.sendHeader('CIMMethod', str(self.factory.method))
        self.sendHeader('CIMObject', str(self.factory.object))

        self.endHeaders()

        # TODO: Figure out why twisted doesn't support unicode.  An
        # exception should be thrown by the str() call if the payload
        # can't be converted to the current codepage.

        self.transport.write(str(self.factory.payload))

    def handleResponse(self, data):
        """Called when all response data has been received."""

        self.factory.response_xml = data

        if self.status == '200':
            self.factory.parseErrorAndResponse(data)

        self.factory.deferred = None
        self.transport.loseConnection()

    def handleStatus(self, version, status, message):
        """Save the status code for processing when we get to the end
        of the headers."""

        self.status = status
        self.message = message

    def handleHeader(self, key, value):
        """Handle header values."""

        import urllib
        if key == 'CIMError':
            self.CIMError = urllib.unquote(value)
        if key == 'PGErrorDetail':
            self.PGErrorDetail = urllib.unquote(value)

    def handleEndHeaders(self):
        """Check whether the status was OK and raise an error if not
        using previously saved header information."""

        if self.status != '200':

            if not hasattr(self, 'cimerror') or \
               not hasattr(self, 'errordetail'):

                self.factory.deferred.errback(
                    CIMError(0, 'HTTP error %s: %s' %
                             (self.status, self.message)))

            else:

                self.factory.deferred.errback(
                    CIMError(0, '%s: %s' % (cimerror, errordetail)))


class WBEMClientFactory(protocol.ClientFactory):
    """Create instances of the WBEMClient class."""

    request_xml = None
    response_xml = None
    xml_header = '<?xml version="1.0" encoding="utf-8" ?>'

    def __init__(self, creds, operation, method, object, payload):
        self.creds = creds
        self.operation = operation
        self.method = method
        self.object = object
        self.payload = payload
        self.protocol = lambda: WBEMClient()
        self.deferred = defer.Deferred()

    def clientConnectionFailed(self, connector, reason):
        if self.deferred is not None:
            reactor.callLater(0, self.deferred.errback, reason)

    def clientConnectionLost(self, connector, reason):
        if self.deferred is not None:
            reactor.callLater(0, self.deferred.errback, reason)

    def imethodcallPayload(self, methodname, localnsp, **kwargs):
        """Generate the XML payload for an intrinsic methodcall."""

        param_list = [pywbem.IPARAMVALUE(x[0], pywbem.tocimxml(x[1]))
                      for x in kwargs.items()]

        payload = pywbem.CIM(
            pywbem.MESSAGE(
                pywbem.SIMPLEREQ(
                    pywbem.IMETHODCALL(
                        methodname,
                        pywbem.LOCALNAMESPACEPATH(
                            [pywbem.NAMESPACE(ns)
                             for ns in string.split(localnsp, '/')]),
                        param_list)),
                '1001', '1.0'),
            '2.0', '2.0')

        return self.xml_header + payload.toxml()

    def methodcallPayload(self, methodname, obj, namespace, **kwargs):
        """Generate the XML payload for an extrinsic methodcall."""

        if isinstance(obj, CIMInstanceName):

            path = obj.copy()

            path.host = None
            path.namespace = None

            localpath = pywbem.LOCALINSTANCEPATH(
                pywbem.LOCALNAMESPACEPATH(
                    [pywbem.NAMESPACE(ns)
                     for ns in string.split(namespace, '/')]),
                path.tocimxml())
        else:
            localpath = pywbem.LOCALCLASSPATH(
                pywbem.LOCALNAMESPACEPATH(
                    [pywbem.NAMESPACE(ns)
                     for ns in string.split(namespace, '/')]),
                obj)

        def paramtype(obj):
            """Return a string to be used as the CIMTYPE for a parameter."""
            if isinstance(obj, cim_types.CIMType):
                return obj.cimtype
            elif type(obj) == bool:
                return 'boolean'
            elif isinstance(obj, StringTypes):
                return 'string'
            elif isinstance(obj, (datetime, timedelta)):
                return 'datetime'
            elif isinstance(obj, (CIMClassName, CIMInstanceName)):
                return 'reference'
            elif isinstance(obj, (CIMClass, CIMInstance)):
                return 'string'
            elif isinstance(obj, list):
                return paramtype(obj[0])
            raise TypeError('Unsupported parameter type "%s"' % type(obj))

        def paramvalue(obj):
            """Return a cim_xml node to be used as the value for a
            parameter."""
            if isinstance(obj, (datetime, timedelta)):
                obj = CIMDateTime(obj)
            if isinstance(obj, (cim_types.CIMType, bool, StringTypes)):
                return cim_xml.VALUE(cim_types.atomic_to_cim_xml(obj))
            if isinstance(obj, (CIMClassName, CIMInstanceName)):
                return cim_xml.VALUE_REFERENCE(obj.tocimxml())
            if isinstance(obj, (CIMClass, CIMInstance)):
                return cim_xml.VALUE(obj.tocimxml().toxml())
            if isinstance(obj, list):
                if isinstance(obj[0], (CIMClassName, CIMInstanceName)):
                    return cim_xml.VALUE_REFARRAY([paramvalue(x) for x in obj])
                return cim_xml.VALUE_ARRAY([paramvalue(x) for x in obj])
            raise TypeError('Unsupported parameter type "%s"' % type(obj))

        param_list = [pywbem.PARAMVALUE(x[0],
                                        paramvalue(x[1]),
                                        paramtype(x[1]))
                      for x in kwargs.items()]

        payload = pywbem.CIM(
            pywbem.MESSAGE(
                pywbem.SIMPLEREQ(
                    pywbem.METHODCALL(methodname,
                                      localpath,
                                      param_list)),
                '1001', '1.0'),
            '2.0', '2.0')

        return self.xml_header + payload.toxml()

    def parseErrorAndResponse(self, data):
        """Parse returned XML for errors, then convert into
        appropriate Python objects."""

        xml = fromstring(data)
        error = xml.find('.//ERROR')

        if error is None:
            self.deferred.callback(self.parseResponse(xml))
            return

        try:
            code = int(error.attrib['CODE'])
        except ValueError:
            code = 0

        self.deferred.errback(CIMError(code, error.attrib['DESCRIPTION']))

    def parseResponse(self, xml):
        """Parse returned XML and convert into appropriate Python
        objects.  Override in subclass"""

        pass

# TODO: Eww - we should get rid of the tupletree, tupleparse modules
# and replace with elementtree based code.

import pywbem.tupletree

class ExecQuery(WBEMClientFactory):
    def __init__(self, creds, QueryLanguage, Query, namespace = 'root/cimv2'):
        self.QueryLanguage = QueryLanguage
        self.Query = Query
        self.namespace = namespace

        payload = self.imethodcallPayload(
            'ExecQuery',
            namespace,
            QueryLanguage = QueryLanguage,
            Query = Query)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'ExecQuery',
            object = namespace,
            payload = payload)

    def __repr__(self):
        return '<%s(/%s:%s) at 0x%x>' % \
               (self.__class__, self.namespace, self.Query, id(self))

    def parseResponse(self, xml):
        tt = [pywbem.tupletree.xml_to_tupletree(tostring(x))
              for x in xml.findall('.//INSTANCE')]

        return [pywbem.tupleparse.parse_instance(x) for x in tt]


class OpenEnumerateInstances(WBEMClientFactory):
    """Factory to produce EnumerateInstances WBEM clients."""

    def __init__(self, creds, classname, namespace='root/cimv2', **kwargs):
        self.classname = classname
        self.namespace = namespace
        self.context = None
        self.property_filter = (None, None)
        self.result_component_key = None

        if not kwargs.get('MaxObjectCount'):
            kwargs['MaxObjectCount'] = DEFAULT_ITER_MAXOBJECTCOUNT

        if 'PropertyFilter' in kwargs:
            self.property_filter = kwargs['PropertyFilter']
            del kwargs['PropertyFilter']

        if 'ResultComponentKey' in kwargs:
            self.result_component_key = kwargs['ResultComponentKey']
            del kwargs['ResultComponentKey']

        payload = self.imethodcallPayload(
            'OpenEnumerateInstances',
            namespace,
            ClassName=CIMClassName(classname),
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation='MethodCall',
            method='OpenEnumerateInstances',
            object=namespace,
            payload=payload)

    def __repr__(self):
        return '<%s(/%s:%s) at 0x%x>' % \
               (self.__class__, self.namespace, self.classname, id(self))

    def parseResponse(self, xml):
        res = []
        part_results = {}
        results_for_monitoring = {}

        for paramvalue in xml.findall('.//PARAMVALUE'):
            str_paramvalue = tostring(paramvalue)
            tuple_paramvalue = pywbem.tupletree.xml_to_tupletree(str_paramvalue)
            part_results.update(pywbem.tupleparse.parse_iter_paramvalue(tuple_paramvalue))

        for x in xml.findall('.//VALUE.INSTANCEWITHPATH'):
            s = tostring(x)
            tt = pywbem.tupletree.xml_to_tupletree(s)
            part_res = pywbem.tupleparse.parse_value_instancewithpath(tt)
            result_element = part_res['VALUE.INSTANCEWITHPATH']

            specific_prop_name, _ = self.property_filter

            specific_prop = False
            if specific_prop_name and specific_prop_name in result_element:
                specific_prop = result_element[specific_prop_name]
            if specific_prop:
                specific_prop_value = None
                component_identifier = None
                if specific_prop_name in result_element:
                    specific_prop_value = str(result_element[specific_prop_name])
                if self.result_component_key in result_element:
                    component_identifier = result_element[
                        self.result_component_key
                    ]

                monitoring_result = {
                    self.classname: {
                        (specific_prop_name, specific_prop_value): {
                            (self.result_component_key, component_identifier):
                                result_element
                        }
                    }
                }

                extend_results(results_for_monitoring, monitoring_result)
            else:
                res.append(result_element)

        if results_for_monitoring:
            part_results.update({'IRETURNVALUE': results_for_monitoring})
        else:
            part_results.update({'IRETURNVALUE': res})
        return OpenEnumerateInstances._getResultParams(part_results)

    @staticmethod
    def _getResultParams(result):
        """Common processing for pull results to separate
           end-of-sequence, enum-context, and entities in IRETURNVALUE.
           Returns tuple of entities in IRETURNVALUE, end_of_sequence,
           and enumeration_context)
        """
        end_of_sequence = False
        enumeration_context = None

        sequence = result.get('EndOfSequence')
        if sequence and isinstance(sequence, six.string_types) and \
                        sequence.lower() in ['true', 'false']:  # noqa: E125
            end_of_sequence = sequence.lower() == 'true'

        context = result.get('EnumerationContext')
        if context and isinstance(context, six.string_types):  # noqa: E125
            enumeration_context = context

        rtn_objects = result.get('IRETURNVALUE') or []

        if not sequence or not context:
            raise CIMError(
                CIM_ERR_INVALID_PARAMETER,
                "EndOfSequence or EnumerationContext required"
            )

        # convert enum context if eos is True
        # Otherwise, returns tuple of enumeration context and namespace
        rtn_ctxt = None if end_of_sequence else enumeration_context

        if rtn_ctxt:
            return (rtn_objects, end_of_sequence, rtn_ctxt)
        else:
            return rtn_objects


class PullInstances(OpenEnumerateInstances):
    def __init__(self, creds, namespace, EnumerationContext,
                 MaxObjectCount, classname, **kwargs):
        self.classname = classname

        self.property_filter = (None, None)
        self.result_component_key = None

        if all(kwargs.get('PropertyFilter', self.property_filter)):
            self.property_filter = kwargs['PropertyFilter']

        if kwargs.get('ResultComponentKey'):
            self.result_component_key = kwargs['ResultComponentKey']

        payload = self.imethodcallPayload(
            'PullInstancesWithPath',
            namespace,
            EnumerationContext=EnumerationContext,
            MaxObjectCount=MaxObjectCount
        )

        WBEMClientFactory.__init__(
            self,
            creds,
            operation='MethodCall',
            method='PullInstancesWithPath',
            object=None,
            payload=payload)


class EnumerateInstances(WBEMClientFactory):
    """Factory to produce EnumerateInstances WBEM clients."""

    def __init__(self, creds, classname, namespace = 'root/cimv2', **kwargs):

        self.classname = classname
        self.namespace = namespace

        payload = self.imethodcallPayload(
            'EnumerateInstances',
            namespace,
            ClassName = CIMClassName(classname),
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'EnumerateInstances',
            object = namespace,
            payload = payload)

    def __repr__(self):
        return '<%s(/%s:%s) at 0x%x>' % \
               (self.__class__, self.namespace, self.classname, id(self))

    @staticmethod
    def parseResponse(xml):
        res = []
        for x in xml.findall('.//VALUE.NAMEDINSTANCE'):
            s = tostring(x)
            tt = pywbem.tupletree.xml_to_tupletree(s)
            r = pywbem.tupleparse.parse_value_namedinstance(tt)
            res.append(r)
        return res

class EnumerateInstanceNames(WBEMClientFactory):
    """Factory to produce EnumerateInstanceNames WBEM clients."""

    def __init__(self, creds, classname, namespace = 'root/cimv2', **kwargs):

        self.classname = classname
        self.namespace = namespace

        payload = self.imethodcallPayload(
            'EnumerateInstanceNames',
            namespace,
            ClassName = CIMClassName(classname),
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'EnumerateInstanceNames',
            object = namespace,
            payload = payload)

    def __repr__(self):
        return '<%s(/%s:%s) at 0x%x>' % \
               (self.__class__, self.namespace, self.classname, id(self))

    def parseResponse(self, xml):

        tt = [pywbem.tupletree.xml_to_tupletree(tostring(x))
              for x in xml.findall('.//INSTANCENAME')]

        names = [pywbem.tupleparse.parse_instancename(x) for x in tt]

        [setattr(n, 'namespace', self.namespace) for n in names]

        return names

class GetInstance(WBEMClientFactory):
    """Factory to produce GetInstance WBEM clients."""

    def __init__(self, creds, instancename, namespace = 'root/cimv2', **kwargs):

        self.instancename = instancename
        self.namespace = namespace

        payload = self.imethodcallPayload(
            'GetInstance',
            namespace,
            InstanceName = instancename,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'GetInstance',
            object = namespace,
            payload = payload)

    def __repr__(self):
        return '<%s(/%s:%s) at 0x%x>' % \
               (self.__class__, self.namespace, self.instancename, id(self))

    def parseResponse(self, xml):

        tt = pywbem.tupletree.xml_to_tupletree(
            tostring(xml.find('.//INSTANCE')))

        return pywbem.tupleparse.parse_instance(tt)

class DeleteInstance(WBEMClientFactory):
    """Factory to produce DeleteInstance WBEM clients."""

    def __init__(self, creds, instancename, namespace = 'root/cimv2', **kwargs):

        self.instancename = instancename
        self.namespace = namespace

        payload = self.imethodcallPayload(
            'DeleteInstance',
            namespace,
            InstanceName = instancename,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'DeleteInstance',
            object = namespace,
            payload = payload)

    def __repr__(self):
        return '<%s(/%s:%s) at 0x%x>' % \
               (self.__class__, self.namespace, self.instancename, id(self))

class CreateInstance(WBEMClientFactory):
    """Factory to produce CreateInstance WBEM clients."""

    # TODO: Implement __repr__ method

    def __init__(self, creds, instance, namespace = 'root/cimv2', **kwargs):

        payload = self.imethodcallPayload(
            'CreateInstance',
            namespace,
            NewInstance = instance,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'CreateInstance',
            object = namespace,
            payload = payload)

    def parseResponse(self, xml):

        tt = pywbem.tupletree.xml_to_tupletree(
            tostring(xml.find('.//INSTANCENAME')))

        return pywbem.tupleparse.parse_instancename(tt)

class ModifyInstance(WBEMClientFactory):
    """Factory to produce ModifyInstance WBEM clients."""

    # TODO: Implement __repr__ method

    def __init__(self, creds, instancename, instance, namespace = 'root/cimv2',
                 **kwargs):

        wrapped_instance = CIMNamedInstance(instancename, instance)

        payload = self.imethodcallPayload(
            'ModifyInstance',
            namespace,
            ModifiedInstance = wrapped_instance,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'ModifyInstance',
            object = namespace,
            payload = payload)

class EnumerateClassNames(WBEMClientFactory):
    """Factory to produce EnumerateClassNames WBEM clients."""

    def __init__(self, creds, namespace = 'root/cimv2', **kwargs):

        self.localnsp = namespace

        payload = self.imethodcallPayload(
            'EnumerateClassNames',
            namespace,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'EnumerateClassNames',
            object = namespace,
            payload = payload)

    def __repr__(self):
        return '<%s(/%s) at 0x%x>' % \
               (self.__class__, self.namespace, id(self))

    def parseResponse(self, xml):

        tt = [pywbem.tupletree.xml_to_tupletree(tostring(x))
              for x in xml.findall('.//CLASSNAME')]

        return [pywbem.tupleparse.parse_classname(x) for x in tt]

class EnumerateClasses(WBEMClientFactory):
    """Factory to produce EnumerateClasses WBEM clients."""

    def __init__(self, creds, namespace = 'root/cimv2', **kwargs):

        self.localnsp = namespace
        self.namespace = namespace

        payload = self.imethodcallPayload(
            'EnumerateClasses',
            namespace,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'EnumerateClasses',
            object = namespace,
            payload = payload)

    def __repr__(self):
        return '<%s(/%s) at 0x%x>' % \
               (self.__class__, self.namespace, id(self))

    def parseResponse(self, xml):

        tt = [pywbem.tupletree.xml_to_tupletree(tostring(x))
              for x in xml.findall('.//CLASS')]

        return [pywbem.tupleparse.parse_class(x) for x in tt]

class GetClass(WBEMClientFactory):
    """Factory to produce GetClass WBEM clients."""

    def __init__(self, creds, classname, namespace = 'root/cimv2', **kwargs):

        self.classname = classname
        self.namespace = namespace

        payload = self.imethodcallPayload(
            'GetClass',
            namespace,
            ClassName = CIMClassName(classname),
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'GetClass',
            object = namespace,
            payload = payload)

    def __repr__(self):
        return '<%s(/%s:%s) at 0x%x>' % \
               (self.__class__, self.namespace, self.classname, id(self))

    def parseResponse(self, xml):

        tt = pywbem.tupletree.xml_to_tupletree(
            tostring(xml.find('.//CLASS')))

        return pywbem.tupleparse.parse_class(tt)

class Associators(WBEMClientFactory):
    """Factory to produce Associators WBEM clients."""

    # TODO: Implement __repr__ method

    def __init__(self, creds, obj, namespace = 'root/cimv2', **kwargs):

        if isinstance(obj, CIMInstanceName):
            kwargs['ObjectName'] = obj
        else:
            kwargs['ObjectName'] = CIMClassName(obj)

        payload = self.imethodcallPayload(
            'Associators',
            namespace,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'Associators',
            object = namespace,
            payload = payload)

class AssociatorNames(WBEMClientFactory):
    """Factory to produce AssociatorNames WBEM clients."""

    # TODO: Implement __repr__ method

    def __init__(self, creds, obj, namespace = 'root/cimv2', **kwargs):

        if isinstance(obj, CIMInstanceName):
            kwargs['ObjectName'] = obj
        else:
            kwargs['ObjectName'] = CIMClassName(obj)

        payload = self.imethodcallPayload(
            'AssociatorNames',
            namespace,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'AssociatorNames',
            object = namespace,
            payload = payload)

    def parseResponse(self, xml):

        if len(xml.findall('.//INSTANCENAME')) > 0:

            tt = [pywbem.tupletree.xml_to_tupletree(tostring(x))
                  for x in xml.findall('.//INSTANCENAME')]

            return [pywbem.tupleparse.parse_instancename(x) for x in tt]

        else:

            tt = [pywbem.tupletree.xml_to_tupletree(tostring(x))
                  for x in xml.findall('.//OBJECTPATH')]

            return [pywbem.tupleparse.parse_objectpath(x)[2] for x in tt]

class References(WBEMClientFactory):
    """Factory to produce References WBEM clients."""

    def __init__(self, creds, obj, namespace = 'root/cimv2', **kwargs):

        if isinstance(obj, CIMInstanceName):
            kwargs['ObjectName'] = obj
        else:
            kwargs['ObjectName'] = CIMClassName(obj)

        payload = self.imethodcallPayload(
            'References',
            namespace,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'References',
            object = namespace,
            payload = payload)

class ReferenceNames(WBEMClientFactory):
    """Factory to produce ReferenceNames WBEM clients."""

    # TODO: Implement __repr__ method

    def __init__(self, creds, obj, namespace = 'root/cimv2', **kwargs):

        if isinstance(obj, CIMInstanceName):
            kwargs['ObjectName'] = obj
        else:
            kwargs['ObjectName'] = CIMClassName(obj)

        payload = self.imethodcallPayload(
            'ReferenceNames',
            namespace,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = 'ReferenceNames',
            object = namespace,
            payload = payload)

    def parseResponse(self, xml):

        if len(xml.findall('.//INSTANCENAME')) > 0:

            tt = [pywbem.tupletree.xml_to_tupletree(tostring(x))
                  for x in xml.findall('.//INSTANCENAME')]

            return [pywbem.tupleparse.parse_instancename(x) for x in tt]

        else:

            tt = [pywbem.tupletree.xml_to_tupletree(tostring(x))
                  for x in xml.findall('.//OBJECTPATH')]

            return [pywbem.tupleparse.parse_objectpath(x)[2] for x in tt]

class InvokeMethod(WBEMClientFactory):
    """Factory to produce InvokeMethod WBEM clients."""

    def __init__(self, creds, MethodName, ObjectName, namespace = 'root/cimv2',
                 **kwargs):

        # Convert string to CIMClassName

        obj = ObjectName

        if isinstance(obj, StringTypes):
            obj = CIMClassName(obj, namespace = namespace)

        if isinstance(obj, CIMInstanceName) and obj.namespace is None:
            obj = ObjectName.copy()
            obj.namespace = namespace

        # Make the method call

        payload = self.methodcallPayload(
            MethodName,
            obj,
            namespace,
            **kwargs)

        WBEMClientFactory.__init__(
            self,
            creds,
            operation = 'MethodCall',
            method = MethodName,
            object = obj,
            payload = payload)

    def parseResponse(self, xml):

        # Return value of method

        result_xml = pywbem.tupletree.xml_to_tupletree(
            tostring(xml.find('.//RETURNVALUE')))

        result_tt = pywbem.tupleparse.parse_any(result_xml)

        result = cim_obj.tocimobj(result_tt[1]['PARAMTYPE'],
                                  result_tt[2])

        # Output parameters

        params_xml = [pywbem.tupletree.xml_to_tupletree(tostring(x))
                      for x in xml.findall('.//PARAMVALUE')]

        params_tt = [pywbem.tupleparse.parse_any(x) for x in params_xml]

        params = {}

        for p in params_tt:
            if p[1] == 'reference':
                params[p[0]] = p[2]
            else:
                params[p[0]] = cim_obj.tocimobj(p[1], p[2])

        return (result, params)
