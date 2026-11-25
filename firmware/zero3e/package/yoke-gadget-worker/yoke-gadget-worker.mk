################################################################################
#
# yoke-gadget-worker
#
################################################################################

YOKE_GADGET_WORKER_SITE = $(BR2_EXTERNAL_YOKE_ZERO3E_PATH)/../../native/yusb
YOKE_GADGET_WORKER_SITE_METHOD = local
YOKE_GADGET_WORKER_LICENSE = MIT

YOKE_GADGET_WORKER_CONF_OPTS = \
	-DCMAKE_BUILD_TYPE=Release

# native/yusb/CMakeLists.txt references ../third_party/lz4 relative to its
# source tree; provide a sibling symlink inside the Buildroot build directory.
define YOKE_GADGET_WORKER_LINK_LZ4
	ln -sfn $(BR2_EXTERNAL_YOKE_ZERO3E_PATH)/../../native/third_party $(@D)/../third_party
endef
YOKE_GADGET_WORKER_PRE_CONFIGURE_HOOKS += YOKE_GADGET_WORKER_LINK_LZ4

define YOKE_GADGET_WORKER_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/yoke-gadget-worker \
		$(TARGET_DIR)/usr/local/libexec/yoke/yoke-gadget-worker
endef

$(eval $(cmake-package))
