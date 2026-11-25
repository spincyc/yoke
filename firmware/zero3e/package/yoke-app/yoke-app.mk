################################################################################
#
# yoke-app
#
################################################################################

YOKE_APP_REPO = $(BR2_EXTERNAL_YOKE_ZERO3E_PATH)/../..
YOKE_APP_DEPENDENCIES = python3 yoke-gadget-worker
YOKE_APP_LICENSE = MIT

define YOKE_APP_INSTALL_TARGET_CMDS
	rm -rf $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages/yoke
	cp -a $(YOKE_APP_REPO)/src/yoke $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages/yoke
	find $(TARGET_DIR)/usr/lib/python$(PYTHON3_VERSION_MAJOR)/site-packages/yoke \
		-type d -name '__pycache__' -exec rm -rf {} +
	$(INSTALL) -d -m 0755 $(TARGET_DIR)/opt/yoke/current/venv/bin
	ln -sfn /usr/bin/python3 $(TARGET_DIR)/opt/yoke/current/venv/bin/python
	ln -sfn /usr/bin/python3 $(TARGET_DIR)/opt/yoke/current/venv/bin/python3
	# Install canonical systemd units and udev/module rules from packaging/zero3e and examples.
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/examples/yoked.service \
		$(TARGET_DIR)/etc/systemd/system/yoked.service
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/examples/yoke-gadget.service \
		$(TARGET_DIR)/etc/systemd/system/yoke-gadget.service
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/packaging/zero3e/computer/udev/70-yoke-raw-gadget.rules \
		$(TARGET_DIR)/etc/udev/rules.d/70-yoke-raw-gadget.rules
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/packaging/zero3e/computer/modules-load.d/yoke-raw-gadget.conf \
		$(TARGET_DIR)/etc/modules-load.d/yoke-raw-gadget.conf
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/packaging/zero3e/computer/yoke-gadget.service.d/20-native-worker.conf \
		$(TARGET_DIR)/etc/systemd/system/yoke-gadget.service.d/20-native-worker.conf
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/packaging/zero3e/device/yoked.service.d/10-zero3e-device.conf \
		$(TARGET_DIR)/etc/systemd/system/yoked.service.d/10-zero3e-device.conf
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/packaging/zero3e/device/modules-load.d/yoke-usbip-host.conf \
		$(TARGET_DIR)/etc/modules-load.d/yoke-usbip-host.conf
	for u in yoke-usbipd.service yoke-usbip-bind@.service yoke-usbip-admission.service yoke-usbip-admission.path; do \
		sed 's/@PORT@/3240/g' $(YOKE_APP_REPO)/packaging/zero3e/device/$$u \
			> $(TARGET_DIR)/etc/systemd/system/$$u; \
		chmod 0644 $(TARGET_DIR)/etc/systemd/system/$$u; \
	done
	# Persistent udev bind rule lives on /data so busid changes survive upgrades.
	ln -sfn /data/etc/udev/70-yoke-usbip-bind.rules \
		$(TARGET_DIR)/etc/udev/rules.d/70-yoke-usbip-bind.rules
	# Ship reference templates for commissioning on-box.
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/packaging/zero3e/device/agent.toml.in \
		$(TARGET_DIR)/usr/share/yoke-zero3e/device-agent.toml.in
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/packaging/zero3e/device/udev/70-yoke-usbip-bind.rules.in \
		$(TARGET_DIR)/usr/share/yoke-zero3e/70-yoke-usbip-bind.rules.in
	$(INSTALL) -D -m 0644 $(YOKE_APP_REPO)/examples/agent-consumer-usb-gadget.toml \
		$(TARGET_DIR)/usr/share/yoke-zero3e/computer-agent.toml.in
endef

$(eval $(generic-package))
