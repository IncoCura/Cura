# Copyright (c) 2019 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.
import json
from queue import Queue
from threading import Event, Thread
from time import time
import os

from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange, ServiceInfo
from PyQt5.QtNetwork import QNetworkRequest, QNetworkAccessManager
from PyQt5.QtCore import pyqtSlot, QUrl, pyqtSignal, pyqtProperty, QObject
from PyQt5.QtGui import QDesktopServices

from cura.CuraApplication import CuraApplication
from cura.PrinterOutputDevice import ConnectionType
from cura.Settings.GlobalStack import GlobalStack # typing
from UM.OutputDevice.OutputDevicePlugin import OutputDevicePlugin
from UM.Logger import Logger
from UM.Signal import Signal, signalemitter
from UM.Version import Version
from UM.Message import Message
from UM.i18n import i18nCatalog

from . import ClusterUM3OutputDevice, LegacyUM3OutputDevice
from .Cloud.CloudOutputDeviceManager import CloudOutputDeviceManager

from typing import Optional

i18n_catalog = i18nCatalog("cura")

##      This plugin handles the connection detection & creation of output device objects for the UM3 printer.
#       Zero-Conf is used to detect printers, which are saved in a dict.
#       If we discover a printer that has the same key as the active machine instance a connection is made.
@signalemitter
class UM3OutputDevicePlugin(OutputDevicePlugin):
    addDeviceSignal = Signal()
    removeDeviceSignal = Signal()
    discoveredDevicesChanged = Signal()
    cloudFlowIsPossible = Signal()

    def __init__(self):
        super().__init__()
        
        self._zero_conf = None
        self._zero_conf_browser = None

        self._application = CuraApplication.getInstance()

        # Create a cloud output device manager that abstracts all cloud connection logic away.
        self._cloud_output_device_manager = CloudOutputDeviceManager()

        # Because the model needs to be created in the same thread as the QMLEngine, we use a signal.
        self.addDeviceSignal.connect(self._onAddDevice)
        self.removeDeviceSignal.connect(self._onRemoveDevice)

        self._application.globalContainerStackChanged.connect(self.reCheckConnections)

        self._discovered_devices = {}
        
        self._network_manager = QNetworkAccessManager()
        self._network_manager.finished.connect(self._onNetworkRequestFinished)

        self._min_cluster_version = Version("4.0.0")
        self._min_cloud_version = Version("5.2.0")

        self._api_version = "1"
        self._api_prefix = "/api/v" + self._api_version + "/"
        self._cluster_api_version = "1"
        self._cluster_api_prefix = "/cluster-api/v" + self._cluster_api_version + "/"

        # Get list of manual instances from preferences
        self._preferences = CuraApplication.getInstance().getPreferences()
        self._preferences.addPreference("um3networkprinting/manual_instances",
                                        "")  # A comma-separated list of ip adresses or hostnames

        self._manual_instances = self._preferences.getValue("um3networkprinting/manual_instances").split(",")

        # Store the last manual entry key
        self._last_manual_entry_key = "" # type: str

        # The zero-conf service changed requests are handled in a separate thread, so we can re-schedule the requests
        # which fail to get detailed service info.
        # Any new or re-scheduled requests will be appended to the request queue, and the handling thread will pick
        # them up and process them.
        self._service_changed_request_queue = Queue()
        self._service_changed_request_event = Event()
        self._service_changed_request_thread = Thread(target=self._handleOnServiceChangedRequests, daemon=True)
        self._service_changed_request_thread.start()

        self._account = self._application.getCuraAPI().account

        # Check if cloud flow is possible when user logs in
        self._account.loginStateChanged.connect(self.checkCloudFlowIsPossible)

        # Check if cloud flow is possible when user switches machines
        self._application.globalContainerStackChanged.connect(self._onMachineSwitched)

        # Listen for when cloud flow is possible 
        self.cloudFlowIsPossible.connect(self._onCloudFlowPossible)

        # Listen if cloud cluster was added
        self._cloud_output_device_manager.addedCloudCluster.connect(self._onCloudPrintingConfigured)

        # Listen if cloud cluster was removed
        self._cloud_output_device_manager.removedCloudCluster.connect(self.checkCloudFlowIsPossible)

        self._start_cloud_flow_message = None # type: Optional[Message]
        self._cloud_flow_complete_message = None # type: Optional[Message]

    def getDiscoveredDevices(self):
        return self._discovered_devices

    def getLastManualDevice(self) -> str:
        return self._last_manual_entry_key

    def resetLastManualDevice(self) -> None:
        self._last_manual_entry_key = ""

    ##  Start looking for devices on network.
    def start(self):
        self.startDiscovery()
        self._cloud_output_device_manager.start()

    def startDiscovery(self):
        self.stop()
        if self._zero_conf_browser:
            self._zero_conf_browser.cancel()
            self._zero_conf_browser = None  # Force the old ServiceBrowser to be destroyed.

        for instance_name in list(self._discovered_devices):
            self._onRemoveDevice(instance_name)

        self._zero_conf = Zeroconf()
        self._zero_conf_browser = ServiceBrowser(self._zero_conf, u'_ultimaker._tcp.local.',
                                                 [self._appendServiceChangedRequest])

        # Look for manual instances from preference
        for address in self._manual_instances:
            if address:
                self.addManualDevice(address)
        self.resetLastManualDevice()

    def reCheckConnections(self):
        active_machine = CuraApplication.getInstance().getGlobalContainerStack()
        if not active_machine:
            return

        um_network_key = active_machine.getMetaDataEntry("um_network_key")

        for key in self._discovered_devices:
            if key == um_network_key:
                if not self._discovered_devices[key].isConnected():
                    Logger.log("d", "Attempting to connect with [%s]" % key)
                    # It should already be set, but if it actually connects we know for sure it's supported!
                    active_machine.addConfiguredConnectionType(self._discovered_devices[key].connectionType.value)
                    self._discovered_devices[key].connect()
                    self._discovered_devices[key].connectionStateChanged.connect(self._onDeviceConnectionStateChanged)
                else:
                    self._onDeviceConnectionStateChanged(key)
            else:
                if self._discovered_devices[key].isConnected():
                    Logger.log("d", "Attempting to close connection with [%s]" % key)
                    self._discovered_devices[key].close()
                    self._discovered_devices[key].connectionStateChanged.disconnect(self._onDeviceConnectionStateChanged)

    def _onDeviceConnectionStateChanged(self, key):
        if key not in self._discovered_devices:
            return
        if self._discovered_devices[key].isConnected():
            # Sometimes the status changes after changing the global container and maybe the device doesn't belong to this machine
            um_network_key = CuraApplication.getInstance().getGlobalContainerStack().getMetaDataEntry("um_network_key")
            if key == um_network_key:
                self.getOutputDeviceManager().addOutputDevice(self._discovered_devices[key])
                self.checkCloudFlowIsPossible()
        else:
            self.getOutputDeviceManager().removeOutputDevice(key)

    def stop(self):
        if self._zero_conf is not None:
            Logger.log("d", "zeroconf close...")
            self._zero_conf.close()
        self._cloud_output_device_manager.stop()

    def removeManualDevice(self, key, address = None):
        if key in self._discovered_devices:
            if not address:
                address = self._discovered_devices[key].ipAddress
            self._onRemoveDevice(key)
            self.resetLastManualDevice()

        if address in self._manual_instances:
            self._manual_instances.remove(address)
            self._preferences.setValue("um3networkprinting/manual_instances", ",".join(self._manual_instances))

    def addManualDevice(self, address):
        if address not in self._manual_instances:
            self._manual_instances.append(address)
            self._preferences.setValue("um3networkprinting/manual_instances", ",".join(self._manual_instances))

        instance_name = "manual:%s" % address
        properties = {
            b"name": address.encode("utf-8"),
            b"address": address.encode("utf-8"),
            b"manual": b"true",
            b"incomplete": b"true",
            b"temporary": b"true"   # Still a temporary device until all the info is retrieved in _onNetworkRequestFinished
        }

        if instance_name not in self._discovered_devices:
            # Add a preliminary printer instance
            self._onAddDevice(instance_name, address, properties)
        self._last_manual_entry_key = instance_name

        self._checkManualDevice(address)

    def _checkManualDevice(self, address):
        # Check if a UM3 family device exists at this address.
        # If a printer responds, it will replace the preliminary printer created above
        # origin=manual is for tracking back the origin of the call
        url = QUrl("http://" + address + self._api_prefix + "system")
        name_request = QNetworkRequest(url)
        self._network_manager.get(name_request)

    def _onNetworkRequestFinished(self, reply):
        reply_url = reply.url().toString()

        if "system" in reply_url:
            if reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) != 200:
                # Something went wrong with checking the firmware version!
                return

            try:
                system_info = json.loads(bytes(reply.readAll()).decode("utf-8"))
            except:
                Logger.log("e", "Something went wrong converting the JSON.")
                return

            address = reply.url().host()
            has_cluster_capable_firmware = Version(system_info["firmware"]) > self._min_cluster_version
            instance_name = "manual:%s" % address
            properties = {
                b"name": (system_info["name"] + " (manual)").encode("utf-8"),
                b"address": address.encode("utf-8"),
                b"firmware_version": system_info["firmware"].encode("utf-8"),
                b"manual": b"true",
                b"machine": str(system_info['hardware']["typeid"]).encode("utf-8")
            }

            if has_cluster_capable_firmware:
                # Cluster needs an additional request, before it's completed.
                properties[b"incomplete"] = b"true"

            # Check if the device is still in the list & re-add it with the updated
            # information.
            if instance_name in self._discovered_devices:
                self._onRemoveDevice(instance_name)
                self._onAddDevice(instance_name, address, properties)

            if has_cluster_capable_firmware:
                # We need to request more info in order to figure out the size of the cluster.
                cluster_url = QUrl("http://" + address + self._cluster_api_prefix + "printers/")
                cluster_request = QNetworkRequest(cluster_url)
                self._network_manager.get(cluster_request)

        elif "printers" in reply_url:
            if reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) != 200:
                # Something went wrong with checking the amount of printers the cluster has!
                return
            # So we confirmed that the device is in fact a cluster printer, and we should now know how big it is.
            try:
                cluster_printers_list = json.loads(bytes(reply.readAll()).decode("utf-8"))
            except:
                Logger.log("e", "Something went wrong converting the JSON.")
                return
            address = reply.url().host()
            instance_name = "manual:%s" % address
            if instance_name in self._discovered_devices:
                device = self._discovered_devices[instance_name]
                properties = device.getProperties().copy()
                if b"incomplete" in properties:
                    del properties[b"incomplete"]
                properties[b"cluster_size"] = len(cluster_printers_list)
                self._onRemoveDevice(instance_name)
                self._onAddDevice(instance_name, address, properties)

    def _onRemoveDevice(self, device_id):
        device = self._discovered_devices.pop(device_id, None)
        if device:
            if device.isConnected():
                device.disconnect()
                try:
                    device.connectionStateChanged.disconnect(self._onDeviceConnectionStateChanged)
                except TypeError:
                    # Disconnect already happened.
                    pass

            self.discoveredDevicesChanged.emit()

    def _onAddDevice(self, name, address, properties):
        # Check what kind of device we need to add; Depending on the firmware we either add a "Connect"/"Cluster"
        # or "Legacy" UM3 device.
        cluster_size = int(properties.get(b"cluster_size", -1))

        printer_type = properties.get(b"machine", b"").decode("utf-8")
        printer_type_identifiers = {
            "9066": "ultimaker3",
            "9511": "ultimaker3_extended",
            "9051": "ultimaker_s5"
        }

        for key, value in printer_type_identifiers.items():
            if printer_type.startswith(key):
                properties[b"printer_type"] = bytes(value, encoding="utf8")
                break
        else:
            properties[b"printer_type"] = b"Unknown"
        if cluster_size >= 0:
            device = ClusterUM3OutputDevice.ClusterUM3OutputDevice(name, address, properties)
        else:
            device = LegacyUM3OutputDevice.LegacyUM3OutputDevice(name, address, properties)

        self._discovered_devices[device.getId()] = device
        self.discoveredDevicesChanged.emit()

        global_container_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if global_container_stack and device.getId() == global_container_stack.getMetaDataEntry("um_network_key"):
            # Ensure that the configured connection type is set.
            global_container_stack.addConfiguredConnectionType(device.connectionType.value)
            device.connect()
            device.connectionStateChanged.connect(self._onDeviceConnectionStateChanged)

    ##  Appends a service changed request so later the handling thread will pick it up and processes it.
    def _appendServiceChangedRequest(self, zeroconf, service_type, name, state_change):
        # append the request and set the event so the event handling thread can pick it up
        item = (zeroconf, service_type, name, state_change)
        self._service_changed_request_queue.put(item)
        self._service_changed_request_event.set()

    def _handleOnServiceChangedRequests(self):
        while True:
            # Wait for the event to be set
            self._service_changed_request_event.wait(timeout = 5.0)

            # Stop if the application is shutting down
            if CuraApplication.getInstance().isShuttingDown():
                return

            self._service_changed_request_event.clear()

            # Handle all pending requests
            reschedule_requests = []  # A list of requests that have failed so later they will get re-scheduled
            while not self._service_changed_request_queue.empty():
                request = self._service_changed_request_queue.get()
                zeroconf, service_type, name, state_change = request
                try:
                    result = self._onServiceChanged(zeroconf, service_type, name, state_change)
                    if not result:
                        reschedule_requests.append(request)
                except Exception:
                    Logger.logException("e", "Failed to get service info for [%s] [%s], the request will be rescheduled",
                                        service_type, name)
                    reschedule_requests.append(request)

            # Re-schedule the failed requests if any
            if reschedule_requests:
                for request in reschedule_requests:
                    self._service_changed_request_queue.put(request)

    ##  Handler for zeroConf detection.
    #   Return True or False indicating if the process succeeded.
    #   Note that this function can take over 3 seconds to complete. Be careful
    #   calling it from the main thread.
    def _onServiceChanged(self, zero_conf, service_type, name, state_change):
        if state_change == ServiceStateChange.Added:
            # First try getting info from zero-conf cache
            info = ServiceInfo(service_type, name, properties = {})
            for record in zero_conf.cache.entries_with_name(name.lower()):
                info.update_record(zero_conf, time(), record)

            for record in zero_conf.cache.entries_with_name(info.server):
                info.update_record(zero_conf, time(), record)
                if info.address:
                    break

            # Request more data if info is not complete
            if not info.address:
                info = zero_conf.get_service_info(service_type, name)

            if info:
                type_of_device = info.properties.get(b"type", None)
                if type_of_device:
                    if type_of_device == b"printer":
                        address = '.'.join(map(lambda n: str(n), info.address))
                        self.addDeviceSignal.emit(str(name), address, info.properties)
                    else:
                        Logger.log("w",
                                   "The type of the found device is '%s', not 'printer'! Ignoring.." % type_of_device)
            else:
                Logger.log("w", "Could not get information about %s" % name)
                return False

        elif state_change == ServiceStateChange.Removed:
            Logger.log("d", "Bonjour service removed: %s" % name)
            self.removeDeviceSignal.emit(str(name))

        return True

    ## Check if the prerequsites are in place to start the cloud flow
    def checkCloudFlowIsPossible(self) -> None:
        Logger.log("d", "Checking if cloud connection is possible...")

        # Pre-Check: Skip if active machine already has been cloud connected or you said don't ask again
        active_machine = self._application.getMachineManager().activeMachine # type: Optional["GlobalStack"]
        if active_machine:
            
            # Check 1A: Printer isn't already configured for cloud
            if ConnectionType.CloudConnection.value in active_machine.configuredConnectionTypes:
                Logger.log("d", "Active machine was already configured for cloud.")
                return
            
            # Check 1B: Printer isn't already configured for cloud
            if active_machine.getMetaDataEntry("cloud_flow_complete", False):
                Logger.log("d", "Active machine was already configured for cloud.")
                return

            # Check 2: User did not already say "Don't ask me again"
            if active_machine.getMetaDataEntry("do_not_show_cloud_message", False):
                Logger.log("d", "Active machine shouldn't ask about cloud anymore.")
                return
        
            # Check 3: User is logged in with an Ultimaker account
            if not self._account.isLoggedIn:
                Logger.log("d", "Cloud Flow not possible: User not logged in!")
                return

            # Check 4: Machine is configured for network connectivity
            if not self._application.getMachineManager().activeMachineHasNetworkConnection:
                Logger.log("d", "Cloud Flow not possible: Machine is not connected!")
                return
            
            # Check 5: Machine has correct firmware version
            firmware_version = self._application.getMachineManager().activeMachineFirmwareVersion # type: str
            if not Version(firmware_version) > self._min_cloud_version:
                Logger.log("d", "Cloud Flow not possible: Machine firmware (%s) is too low! (Requires version %s)",
                                firmware_version,
                                self._min_cloud_version)
                return
            
            Logger.log("d", "Cloud flow is possible!")
            self.cloudFlowIsPossible.emit()

    def _onCloudFlowPossible(self) -> None:
        # Cloud flow is possible, so show the message
        if not self._start_cloud_flow_message:
            self._start_cloud_flow_message = Message(
                text = i18n_catalog.i18nc("@info:status", "Send and monitor print jobs from anywhere using your Ultimaker account."),
                lifetime = 0,
                image_source = QUrl.fromLocalFile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                                               "resources", "svg", "cloud-flow-start.svg")),
                image_caption = i18n_catalog.i18nc("@info:status", "Connect to Ultimaker Cloud"),
                option_text = i18n_catalog.i18nc("@action", "Don't ask me again for this printer."),
                option_state = False
            )
            self._start_cloud_flow_message.addAction("", i18n_catalog.i18nc("@action", "Get started"), "", "")
            self._start_cloud_flow_message.optionToggled.connect(self._onDontAskMeAgain)
            self._start_cloud_flow_message.actionTriggered.connect(self._onCloudFlowStarted)
            self._start_cloud_flow_message.show()
            return

    def _onCloudPrintingConfigured(self) -> None:
        if self._start_cloud_flow_message:
            self._start_cloud_flow_message.hide()
            self._start_cloud_flow_message = None
        
        # Show the successful pop-up
        if not self._start_cloud_flow_message:
            self._cloud_flow_complete_message = Message(
                text = i18n_catalog.i18nc("@info:status", "You can now send and monitor print jobs from anywhere using your Ultimaker account."),
                lifetime = 30,
                image_source = QUrl.fromLocalFile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                                               "resources", "svg", "cloud-flow-completed.svg")),
                image_caption = i18n_catalog.i18nc("@info:status", "Connected!")
            )
            # Don't show the review connection link if we're not on the local network
            if self._application.getMachineManager().activeMachineHasNetworkConnection:
                self._cloud_flow_complete_message.addAction("", i18n_catalog.i18nc("@action", "Review your connection"), "", "", 1) # TODO: Icon
                self._cloud_flow_complete_message.actionTriggered.connect(self._onReviewCloudConnection)
            self._cloud_flow_complete_message.show()

            # Set the machine's cloud flow as complete so we don't ask the user again and again for cloud connected printers
            active_machine = self._application.getMachineManager().activeMachine
            if active_machine:
                active_machine.setMetaDataEntry("do_not_show_cloud_message", True)
            return

    def _onDontAskMeAgain(self, checked: bool) -> None:
        active_machine = self._application.getMachineManager().activeMachine # type: Optional["GlobalStack"]
        if active_machine:
            active_machine.setMetaDataEntry("do_not_show_cloud_message", checked)
            if checked:
                Logger.log("d", "Will not ask the user again to cloud connect for current printer.")
        return

    def _onCloudFlowStarted(self, messageId: str, actionId: str) -> None:
        address = self._application.getMachineManager().activeMachineAddress # type: str
        if address:
            QDesktopServices.openUrl(QUrl("http://" + address + "/cloud_connect"))
            if self._start_cloud_flow_message:
                self._start_cloud_flow_message.hide()
                self._start_cloud_flow_message = None
        return
    
    def _onReviewCloudConnection(self, messageId: str, actionId: str) -> None:
        address = self._application.getMachineManager().activeMachineAddress # type: str
        if address:
            QDesktopServices.openUrl(QUrl("http://" + address + "/settings"))
        return

    def _onMachineSwitched(self) -> None:
        if self._start_cloud_flow_message is not None:
            self._start_cloud_flow_message.hide()
            self._start_cloud_flow_message = None
        if self._cloud_flow_complete_message is not None:
            self._cloud_flow_complete_message.hide()
            self._cloud_flow_complete_message = None

        self.checkCloudFlowIsPossible()
