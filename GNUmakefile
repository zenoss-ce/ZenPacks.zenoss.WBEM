##############################################################################
#
# Copyright (C) Zenoss, Inc. 2012, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

PYTHON=$(shell which python)
HERE=$(PWD)
PYWBEM_DIR=$(HERE)/src/pywbem
ZP_DIR=$(HERE)/ZenPacks/zenoss/WBEM
LIB_DIR=$(ZP_DIR)/lib
BIN_DIR=$(ZP_DIR)/bin
DOC_DIR=$(HERE)/docs

.PHONY: docs


default: egg

egg:
	# setup.py will call 'make build' before creating the egg
	python setup.py bdist_egg

build:
	cd $(PYWBEM_DIR) ; \
		PYTHONPATH="$(PYTHONPATH):$(LIB_DIR)" \
		$(PYTHON) setup.py install \
		--install-lib="$(LIB_DIR)" \
		--install-scripts="$(BIN_DIR)"

clean:
	rm -rf lib build dist *.egg-info
	cd $(PYWBEM_DIR) ; rm -rf build dist *.egg-info

test:
	runtests -v ZenPacks.zenoss.WBEM

docs:
	make -C $(DOC_DIR)
