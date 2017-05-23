##############################################################################
#
# Copyright (C) Zenoss, Inc. 2012, 2017, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

import calendar

from twisted.internet import ssl, reactor
from twisted.internet.error import ConnectionRefusedError, TimeoutError


def addLocalLibPath():
    """
    Helper to add the ZenPack's lib directory to PYTHONPATH.
    """
    import os
    import site

    site.addsitedir(os.path.join(os.path.dirname(__file__), 'lib'))


def result_errmsg(result):
    """Return a useful error message string given a twisted errBack result."""
    try:
        from pywbem.cim_operations import CIMError

        if result.type == ConnectionRefusedError:
            return 'connection refused. Check IP and zWBEMPort'
        elif result.type == TimeoutError:
            return 'connection timeout. Check IP and zWBEMPort'
        elif result.type == CIMError:
            if '401' in result.value.args[1]:
                return 'login failed. Check zWBEMUsername and zWBEMPassword'
            else:
                return result.value.args[1]
        else:
            return result.getErrorMessage()
    except AttributeError:
        pass

    return str(result)


def create_connection(config, wbemClass):
    """Create SSL or TCP connection to collect data for monitoring and modeling."""
    if config.zWBEMUseSSL is True:
        reactor.connectSSL(
            host=config.manageIp,
            port=int(config.zWBEMPort),
            factory=wbemClass,
            contextFactory=ssl.ClientContextFactory())
    else:
        reactor.connectTCP(
            host=config.manageIp,
            port=int(config.zWBEMPort),
            factory=wbemClass)


def convert_to_timestamp(object):
        """Convert value of CIMDateTime object to timestamp."""
        return calendar.timegm(object.datetime.utctimetuple())
