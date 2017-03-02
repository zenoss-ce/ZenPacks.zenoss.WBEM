##############################################################################
#
# Copyright (C) Zenoss, Inc. 2017, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

from ZenPacks.zenoss.WBEM.utils import addLocalLibPath
addLocalLibPath()

from pywbem import CIMError
from pywbem import twisted_client
try:
    from elementtree.ElementTree import fromstring
except ImportError, arg:
    from xml.etree.ElementTree import fromstring

from pywbem.twisted_client import (
    EnumerateClassNames as BaseEnumerateClassNames,
    EnumerateClasses as BaseEnumerateClasses,
    EnumerateInstanceNames as BaseEnumerateInstanceNames,
    EnumerateInstances as BaseEnumerateInstances
)


class HandleResponseMixin():
    """Override base parseErrorAndResponse from pywbem.twisted_client module
    to catch XML parsing error"""

    def parseErrorAndResponse(self, data):
        """Parse returned XML for errors, then convert into
        appropriate Python objects."""
        try:
            xml = fromstring(data)
        except Exception:
            self.deferred.errback(
                CIMError(
                    0, 'Incorrect XML response for {0}'.format(self.classname)
                )
            )
            return

        error = xml.find('.//ERROR')

        if error is None:
            self.deferred.callback(self.parseResponse(xml))
            return

        try:
            code = int(error.attrib['CODE'])
        except ValueError:
            code = 0

        self.deferred.errback(CIMError(code, error.attrib['DESCRIPTION']))


class EnumerateInstances(HandleResponseMixin, BaseEnumerateInstances):
    pass


class EnumerateInstanceNames(HandleResponseMixin, BaseEnumerateInstanceNames):
    pass


class EnumerateClasses(HandleResponseMixin, BaseEnumerateClasses):
    pass


class EnumerateClassNames(HandleResponseMixin, BaseEnumerateClassNames):
    pass


class NewWBEMClient(object, twisted_client.WBEMClient):

    def rawDataReceived(self, data):
        """
        Override this method to cancel deferred timeout
        in case we got some response from resource side.
        """
        if hasattr(self.factory, 'deferred_timeout'):
            if self.factory.deferred_timeout.active():
                self.factory.deferred_timeout.cancel()
        super(NewWBEMClient, self).rawDataReceived(data)


twisted_client.WBEMClient = NewWBEMClient
