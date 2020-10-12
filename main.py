import logging
import argparse

parser = argparse.ArgumentParser(description='Ruft Daten von E3DC-Systemen mittels RSCP ab')
parser.add_argument('-e', '--export', const=True, default=False, nargs='?',
                    help='Exportstart mit Programmstart')
parser.add_argument('--hide', const=True, default=False, nargs='?',
                    help='Programm verstecken')
parser.add_argument("-v", "--verbose", const=True, default=False, nargs='?', help='Erhöhen des Loglevels')

args = parser.parse_args()
if args.verbose:
    loglevel = logging.DEBUG
else:
    loglevel = logging.ERROR

logger = logging.getLogger(__name__)
logger.setLevel(loglevel)

# create console handler and set level to debug
ch = logging.StreamHandler()
ch.setLevel(loglevel)

# create formatter
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# add formatter to ch
ch.setFormatter(formatter)

# add ch to logger
logger.addHandler(ch)

logger.debug('Programmstart')


from socket import setdefaulttimeout
import csv
from export import E3DCExport
import configparser
import datetime
import hashlib
import json
import os
import re
import threading
import time
import traceback
import random
import base64

import pytz as pytz
import requests
import wx
import wx.dataview

from e3dc._rscp_dto import RSCPDTO
from e3dc._rscp_exceptions import RSCPCommunicationError
from e3dc.rscp_helper import rscp_helper
from e3dc.rscp_tag import RSCPTag
from e3dc.rscp_type import RSCPType
from e3dcwebgui import E3DCWebGui

from gui import MainFrame
import paho.mqtt.client as paho

try:
    import thread
except ImportError:
    import _thread as thread


class E3DCGui(rscp_helper):
    pass

class MessageBox(wx.Dialog):
    def __init__(self, parent, title, value):
        wx.Dialog.__init__(self, parent, title=title)
        text = wx.TextCtrl(self, style=wx.TE_READONLY | wx.BORDER_NONE | wx.TE_MULTILINE)
        text.SetValue(value)
        text.SetBackgroundColour(self.GetBackgroundColour())
        self.ShowModal()
        self.Destroy()

class Frame(MainFrame):
    _serverApp = None
    _extsrcavailable = 0
    _gui = None
    _time_format = '%d.%m.%Y %H:%M:%S.%f'
    _connectiontype = None
    _websocketaddr = None
    _connected = None
    _updateRunning = None
    _mbsSettings = {}
    debug = False
    _e3dcexportFrame = None
    _e3dcExportPaths = []
    _AutoExportStarted = False
    _args = None

    def __init__(self, parent, args):
        logger.info('Programm gestartet, init')
        self._args = args
        self.clear_values()

        MainFrame.__init__(self, parent)

        logger.info('Oberfläche geladen')

        self.ConfigFilename = 'rscpe3dc.conf.ini'

        def load_timezones():
            logger.info('Lade Zeitzonen geladen')
            for tz in pytz.common_timezones:
                self.cbTimezone.Append(str(tz))
            logger.info('Zeitzonen geladen')

        threading.Thread(target=load_timezones, args=()).start()

        self.loadConfig()

        setdefaulttimeout(2)

        logger.info('Konfigurationsdatei geladen')

        self.cbConfigVerbindungsart.SetValue(self._connectiontype)

        self.Bind(wx.EVT_BUTTON, self.bTestClick, self.bTest)
        self.Bind(wx.EVT_BUTTON, self.bSaveClick, self.bSave)
        self.Bind(wx.EVT_BUTTON, self.bUpdateClick, self.bUpdate)

        self.gDCB_row_voltages = None
        self.gDCB_row_temp = None

        self._gui = None

        self._wsthread = threading.Thread(target=self.check_e3dcwebgui, args=())
        self._wsthread.start()

        def preConnect():
            try:
                g = self.gui
                if self._args.export:
                    logger.debug('Export bei Programmstart aktiviert')
                    self.bUploadStartOnClick(None)
            except:
                pass

        threading.Thread(target=preConnect, args=()).start()

        self._autothread = threading.Thread(target=self.autoUpdate, args=())
        self._autothread.start()

        logger.info('Init abgeschlossen')

    def scAutoUpdateOnChange( self, event ):
        autoupdate = self.scAutoUpdate.GetValue()
        if autoupdate > 0 and self._autothread is None:
            self._autothread = threading.Thread(target=self.autoUpdate, args=())
            self._autothread.start()

    def autoUpdate(self):
        while True:
            autoupdate = self.scAutoUpdate.GetValue()
            if autoupdate == 0:
                self._autothread = None
                return False
            else:
                self.pMainChanged()
                time.sleep(autoupdate)

    def bUploadStartOnClick( self, event ):
        if not self._AutoExportStarted:
            self._AutoExportStarted = True
            self.bUploadStart.SetLabel('Beenden!')

            self._autoexportthread = threading.Thread(target=self.StartAutoExport, args=())
            self._autoexportthread.start()
        else:
            self._AutoExportStarted = False

    def StartAutoExport(self):
        def mqtt_connect(broker,port):
            logger.debug('Verbinde mit MQTT-Broker ' + broker + ':' + str(port))
            mqttclient = paho.Client("RSCPGui")
            mqttclient.connect(broker, port)
            return mqttclient

        try:
            logger.debug('Starte automatischen Export')
            if len(self._e3dcExportPaths) > 0:
                logger.debug('Es sind ' + str(len(self._e3dcExportPaths)) + ' Datenfelder zum Export vorgesehen')
            else:
                logger.debug('Es wurden keine Exporfelder definiert!')

            csvwriter = None
            csvfile = None

            csvactive = self.chUploadCSV.GetValue()
            csvfilename = self.fpUploadCSV.GetPath()

            jsonactive = self.chUploadJSON.GetValue()
            jsonfilename = self.fpUploadJSON.GetPath()

            mqttactive = self.chUploadMQTT.GetValue()
            mqttbroker = self.txtUploadMQTTBroker.GetValue()
            mqttport = int(self.txtUploadMQTTPort.GetValue())
            mqttqos = self.scUploadMQTTQos.GetValue()
            mqttretain = self.chUploadMQTTRetain.GetValue()
            mqttclient = mqtt_connect(mqttbroker, mqttport) if mqttactive else None

            httpactive = self.chUploadHTTP.GetValue()
            httpurl = self.txtUploadHTTPURL.GetValue()

            intervall = self.scUploadIntervall.GetValue()


            if csvactive:
                csvfile = open(csvfilename, 'a', newline='')
                fields = self._e3dcExportPaths.copy()
                fields.insert(0,'datetime')
                fields.insert(0,'ts')
                csvwriter = csv.DictWriter(csvfile, fieldnames = fields)
                csvwriter.writeheader()

            while self._AutoExportStarted:
                laststart = time.time()
                logger.debug('Exportiere Daten (autoexport)')
                try:
                    values = self.getUploadDataFromPath()
                    values['ts'] = time.time()
                    values['datetime'] = datetime.datetime.now().isoformat()
                    if csvactive:
                        try:
                            logger.debug('Exportiere in CSV-Datei ' + csvfilename)
                            csvwriter.writerow(values)
                            csvfile.flush()
                        except:
                            logger.exception('Fehler beim Export in CSV-Datei')

                    if jsonactive:
                        try:
                            logger.debug('Exportiere in JSON-Datei ' + jsonfilename)
                            with open(jsonfilename, 'w') as jsonfile:
                                json.dump(values, jsonfile)
                        except:
                            logger.exception('Fehler beim Export in JSON-Datei')

                    if mqttactive:
                        try:
                            logger.debug('Exportiere nach MQTT')
                            for key in values.keys():
                                if key not in ('ts', 'datetime'):
                                    topic = '/' + key
                                    res,mid = mqttclient.publish(topic, values[key], mqttqos, mqttretain)
                                    if res != 0:
                                        mqttclient.disconnect()
                                        logger.error('Fehler bei Export an MQTT bei Topic ' + topic + ' Errorcode: ' + str(res))
                                        mqttclient = mqtt_connect(mqttbroker, mqttport)
                        except:
                            logger.exception('Fehler beim Export nach MQTT')

                    if httpactive:
                        try:
                            logger.debug('Exportiere an Http-Url ' + httpurl)
                            r = requests.post(httpurl, json=values)
                            r.raise_for_status()
                            logger.debug('Export an URL Erfolgreich ' + str(r.status_code))
                            logger.debug('Response: ' + r.text)
                        except:
                            logger.exception('Fehler beim Export in Http')


                except:
                    logger.exception('Fehler beim Abruf der Exportdaten')

                diff = time.time() - laststart
                if diff < intervall:
                    wait = intervall - diff
                    logger.debug('Warte ' + str(wait) + 's')
                    time.sleep(wait)

            if csvactive:
                csvfile.close()


        except:
            logger.exception('Fehler beim automatischen Export')

        self._AutoExportStarted = False
        self.bUploadStart.SetLabel('Starten!')

    def getUploadDataFromPath(self):
        def getDataFromPath(teile, data):
            if data is not None:
                if isinstance(data, dict):
                    if teile[0] in data.keys():
                        if len(teile) == 1:
                            return data[teile[0]]
                        else:
                            return getDataFromPath(teile[1:], data[teile[0]])
                elif isinstance(data, list):
                    if data[int(teile[0])] is not None:
                        if len(teile) == 1:
                            return data[int(teile[0])]
                        else:
                            return getDataFromPath(teile[1:], data[int(teile[0])])
                else:
                    logger.warning('Element not Found ' + '/'.join(teile))

        ems_data = None
        bat_data = None

        values = {}

        for path in self._e3dcExportPaths:
            logger.debug('Ermittle Pfad aus ' + path)
            teile = path.split('/')
            if teile[0] == 'E3DC':
                if teile[1] == 'EMS_DATA':
                    try:
                        if not ems_data:
                            ems_data = self.gui.get_data(self.gui.getEMSData(), True).asDict()

                        values[path] = getDataFromPath(teile[2:], ems_data)
                    except:
                        logger.exception('Fehler beim Abruf von INFO')
                elif teile[1] == 'BAT_DATA':
                    try:
                        if not bat_data:
                            bat_data = self.gui.get_data(self.gui.getBatDcbData(bat_index=int(teile[2])), True).asDict()
                        values[path] = getDataFromPath(teile[3:], bat_data)
                    except:
                        logger.exception('Fehler beim Abruf von INFO')
            else:
                logger.debug('Pfadangabe falsch: ' + path)

        return values



    def bUploadSetDataOnClick( self, event ):
        self._e3dcexportFrame = E3DCExport(self, paths = self._e3dcExportPaths)
        self._e3dcexportFrame.Bind(wx.EVT_CLOSE, self.ExportFrameOnClose)
        self._e3dcexportFrame.Show()

    def ExportFrameOnClose(self, event ):
        logger.debug('Exportfenster wurde geschlossen')
        self._e3dcExportPaths = self._e3dcexportFrame.getExportPaths()

        self.stUploadCount.SetLabel('Es wurden ' + str(len(self._e3dcExportPaths)) + ' Datenfelder angewählt')

        for path in self._e3dcExportPaths:
            logger.debug('Pfad: ' + path + ' angewählt und gespeichert')

        event.Skip()


    def pMainChanged( self, event = None):
        page = self.pMainregister.GetCurrentPage()

        name = page.GetName()

        if name in ('DCDC','INFO','BAT','PVI','EMS','WB','PM') and not self._updateRunning:
            self._updateRunning = True
            self.disableButtons()
            self.gaUpdate.SetValue(0)

            try:
                logger.debug('Aktualisiere ' + name)
                if self.gui:
                    if page == self.pDCDC:
                        try:
                            self.fill_dcdc()
                        except:
                            logger.exception('Fehler beim Abruf der DCDC-Daten')
                    elif page == self.pMain:
                        try:
                            self.fill_info()
                        except:
                            logger.exception('Fehler beim Abruf der INFO-Daten')
                    elif page == self.pBAT:
                        try:
                            selected = self.cbBATIndex.GetSelection()
                            if selected in [wx.NOT_FOUND, '', None, False]:
                                selected = 0

                            self.fill_bat()

                            if selected != wx.NOT_FOUND:
                                if self.cbBATIndex.GetCount() > selected:
                                    self.cbBATIndex.SetSelection(selected)
                                    self.fill_bat_index(selected)
                        except:
                            logger.exception('Fehler beim Abruf der BAT-Daten')
                    elif page == self.pPVI:
                        try:
                            selected = self.chPVIIndex.GetSelection()
                            if selected in [wx.NOT_FOUND, '', None, False]:
                                selected = 0

                            self.fill_pvi()

                            if selected != wx.NOT_FOUND:
                                if self.chPVIIndex.GetCount() > selected:
                                    self.chPVIIndex.SetSelection(selected)
                                    self.fill_pvi_index(selected)
                        except:
                            logger.exception('Fehler beim Abruf der PVI-Daten')
                    elif page == self.pEMS:
                        try:
                            self.fill_ems()
                            self.fill_mbs()
                        except:
                            logger.exception('Fehler beim Abruf der EMS-Daten')
                    elif page == self.pPM:
                        try:
                            self.fill_pm()
                        except:
                            logger.exception('Fehler beim Abruf der PM-Daten')
                    elif page == self.pWB:
                        try:
                            selected = self.cbWallbox.GetSelection()
                            if selected in [wx.NOT_FOUND, '', None, False]:
                                selected = 0

                            self.fill_wb()

                            if selected != wx.NOT_FOUND:
                                if self.cbWallbox.GetCount() > selected:
                                    self.cbWallbox.SetSelection(selected)
                                    self.fill_wb_index(selected)
                        except:
                            logger.exception('Fehler beim Abruf der WB-Daten')
                else:
                    logger.warning('Konfiguration unvollständig, Verbindung nicht möglich')
            except:
                logger.exception('Fehler beim Aktualisieren von ' + name)
            self.gaUpdate.SetValue(100)
            self.enableButtons()
            self._updateRunning = False

    def tinycode(self, key, text, reverse=False):
        "(de)crypt stuff"
        rand = random.Random(key).randrange

        if reverse:
            text = base64.b64decode(text.encode('utf-8')).decode('utf-8', 'ignore')
        text = ''.join([chr(ord(elem) ^ rand(256)) for elem in text])

        if not reverse:
            text = base64.b64encode(text.encode('utf-8')).decode('utf-8', 'ignore')

        return text

    def loadConfig(self):
        logger.info('Lade Konfigurationsdatei ' + self.ConfigFilename)

        config = configparser.ConfigParser()
        config.read(self.ConfigFilename)

        if 'Login' in config:

            if 'username' in config['Login']:
                self.txtUsername.SetValue(config['Login']['username'])
            if 'password' in config['Login']:
                if config['Login']['password'][0] == '@':
                    pwd = self.tinycode('rscpgui', config['Login']['password'][1:], True)
                else:
                    pwd = config['Login']['password']
                self.txtPassword.SetValue(pwd)
            if 'address' in config['Login']:
                self.txtIP.SetValue(config['Login']['address'])
            if 'rscppassword' in config['Login']:
                if config['Login']['rscppassword'][0] == '@':
                    pwd = self.tinycode('rscpgui_rscppass', config['Login']['rscppassword'][1:], True)
                else:
                    pwd = config['Login']['rscppassword']
                self.txtRSCPPassword.SetValue(pwd)
            if 'seriennummer' in config['Login']:
                self.txtConfigSeriennummer.SetValue(config['Login']['seriennummer'])
            if 'websocketaddr' in config['Login']:
                self._websocketaddr = config['Login']['websocketaddr']
            if 'connectiontype' in config['Login']:
                self._connectiontype = config['Login']['connectiontype']
            if 'autoupdate' in config['Login']:
                self.scAutoUpdate.SetValue(config['Login']['autoupdate'])

        self.stUploadCount.SetLabel('Keine Datenfelder angewählt')

        if 'Export' in config:
            if 'csv' in config['Export']:
                self.chUploadCSV.SetValue(True if config['Export']['csv'].lower() in ('true','1','ja') else False)
            if 'csvfile' in config['Export']:
                self.fpUploadCSV.SetPath(config['Export']['csvfile'])
            if 'json' in config['Export']:
                self.chUploadJSON.SetValue(True if config['Export']['json'].lower() in ('true','1','ja') else False)
            if 'jsonfile' in config['Export']:
                self.fpUploadJSON.SetPath(config['Export']['jsonfile'])
            if 'mqtt' in config['Export']:
                self.chUploadMQTT.SetValue(True if config['Export']['mqtt'].lower() in ('true','1','ja') else False)
            if 'mqttbroker' in config['Export']:
                self.txtUploadMQTTBroker.SetValue(config['Export']['mqttbroker'])
            else:
                self.txtUploadMQTTBroker.SetValue('localhost')

            if 'mqttport' in config['Export']:
                self.txtUploadMQTTPort.SetValue(config['Export']['mqttport'])
            else:
                self.txtUploadMQTTPort.SetValue('1883')

            if 'mqttqos' in config['Export']:
                self.scUploadMQTTQos.SetValue(int(config['Export']['mqttqos']))
            else:
                self.scUploadMQTTQos.SetValue(0)

            if 'mqttretain' in config['Export']:
                self.chUploadMQTTRetain.SetValue(True if config['Export']['mqttretain'].lower() in ('true','1','ja') else False)

            if 'http' in config['Export']:
                self.chUploadHTTP.SetValue(True if config['Export']['http'].lower() in ('true','1','ja') else False)
            if 'httpurl' in config['Export']:
                self.txtUploadHTTPURL.SetValue(config['Export']['httpurl'])
            if 'intervall' in config['Export']:
                self.scUploadIntervall.SetValue(int(config['Export']['intervall']))
            if 'paths' in config['Export']:
                self._e3dcExportPaths = config['Export']['paths'].split(',')
                self.stUploadCount.SetLabel('Es wurden ' + str(len(self._e3dcExportPaths)) + ' Datenfelder angewählt')



        logger.info('Konfigurationsdatei geladen')
                
        if self._websocketaddr in ('',None):
            self._websocketaddr = 'wss://s10.e3dc.com/ws'

        if self._connectiontype not in ('auto','direkt','web'):
            self._connectiontype = 'auto'

    def saveConfig(self):
        logger.info('Speichere Konfigurationsdatei ' + self.ConfigFilename)

        config = configparser.ConfigParser()
        config.read(self.ConfigFilename)
        config['Login'] = {'username':self.txtUsername.GetValue(),
                           'password':'@' + self.tinycode('rscpgui', self.txtPassword.GetValue()),
                           'address':self.txtIP.GetValue(),
                           'rscppassword':'@' + self.tinycode('rscpgui_rscppass', self.txtRSCPPassword.GetValue()),
                           'seriennummer':self.txtConfigSeriennummer.GetValue(),
                           'websocketaddr':self._websocketaddr,
                           'connectiontype':self._connectiontype,
                           'autoupdate':self.scAutoUpdate.GetValue()}

        config['Export'] = {'csv':self.chUploadCSV.GetValue(),
                            'csvfile':self.fpUploadCSV.GetPath(),
                            'json':self.chUploadJSON.GetValue(),
                            'jsonfile':self.fpUploadJSON.GetPath(),
                            'mqtt':self.chUploadMQTT.GetValue(),
                            'mqttbroker':self.txtUploadMQTTBroker.GetValue(),
                            'mqttport':self.txtUploadMQTTPort.GetValue(),
                            'mqttqos':self.scUploadMQTTQos.GetValue(),
                            'mqttretain':self.chUploadMQTTRetain.GetValue(),
                            'http':self.chUploadHTTP.GetValue(),
                            'httpurl':self.txtUploadHTTPURL.GetValue(),
                            'intervall':self.scUploadIntervall.GetValue(),
                            'paths':','.join(self._e3dcExportPaths)}

        with open(self.ConfigFilename, 'w') as configfile:
            config.write(configfile)

        logger.info('Konfigurationsdatei gespeichert')

    @property
    def connectiontype(self):
        if isinstance(self._gui, E3DCGui):
            return 'direkt'
        elif isinstance(self._gui, E3DCWebGui):
            return 'web'
        else:
            return None

    @property
    def gui(self):
        def test_connection(testgui):
            requests = []
            requests.append(RSCPTag.INFO_REQ_SERIAL_NUMBER)
            requests.append(RSCPTag.INFO_REQ_IP_ADDRESS)
            return testgui.get_data(requests, True)

        if self._gui:
            if self._username == self.txtUsername.GetValue() and self._password == self.txtPassword.GetValue() and \
                    self._address == self.txtIP.GetValue() and self._rscppass == self.txtRSCPPassword.GetValue() and \
                    self._seriennummer == self.txtConfigSeriennummer.GetValue() and \
                    self._connectiontype == self.cbConfigVerbindungsart.GetValue():
                return self._gui

        self._username = self.txtUsername.GetValue()
        self._password = self.txtPassword.GetValue()
        self._address = self.txtIP.GetValue()
        self._rscppass = self.txtRSCPPassword.GetValue()
        self._seriennummer = self.txtConfigSeriennummer.GetValue()
        self._connectiontype = self.cbConfigVerbindungsart.GetValue()

        if isinstance(self._gui, E3DCWebGui):
            self._gui.e3dc.ws.close()

        if self._username and self._password and self._connectiontype == 'auto':
            logger.debug("Ermittle beste Verbindungsart (Verbindungsart auto)")
            seriennummer = self._seriennummer
            address = self._address
            testgui = None
            testgui_web = None
            if self._username and self._password and not seriennummer:
                if self._username and self._password and address and self._rscppass:
                    try:
                        testgui = E3DCGui(self._username, self._password, address, self._rscppass)
                        seriennummer = repr(test_connection(testgui)['INFO_SERIAL_NUMBER'])
                    except:
                        pass

                if not seriennummer:
                    ret = self.getSerialnoFromWeb(self._username, self._password)
                    if len(ret) == 1:
                        seriennummer = self.getSNFromNumbers(ret[0]['serialno'])

            if self._username and self._password and self._rscppass and seriennummer and not address and self._websocketaddr:
                logger.debug('Versuche IP-Adresse zu ermitteln')
                try:
                    testgui = E3DCWebGui(self._username, self._password, seriennummer)
                    ip = repr(test_connection(testgui)['INFO_IP_ADDRESS'])
                    if ip:
                        address = ip
                        logger.debug('IP-Adresse konnte ermittelt werden: ' + ip)
                        testgui_web = testgui
                    else:
                        raise Exception('IP-Adresse konnte nicht ermittelt werden, kein Inahlt')
                except:
                    testgui = None
                    logger.exception('Bei der Ermittlung der IP-Adresse ist ein Fehler aufgetreten')


            if self._username and self._password and address and self._rscppass:
                logger.debug('Teste direkte Verbindungsart')

                if not isinstance(testgui, E3DCGui):
                    testgui = E3DCGui(self._username, self._password, address, self._rscppass)
                    
                try:
                    result = test_connection(testgui)
                    if not seriennummer:
                        seriennummer = repr(result['INFO_SERIAL_NUMBER'])
                    logger.info('Verwende Direkte Verbindung / Verbindung mit System ' + repr(result['INFO_SERIAL_NUMBER']) + ' / ' + repr(result['INFO_IP_ADDRESS']))
                except ConnectionResetError as e:
                    logger.warning("Direkte Verbindung fehlgeschlagen (Socket) error({0}): {1}".format(e.errno, e.strerror))
                    testgui = None
                except RSCPCommunicationError as e:
                    logger.warning("Direkte Verbindung fehlgeschlagen (RSCP)")
                    testgui = None
                except:
                    logger.exception('Fehler beim Aufbau der direkten Verbindung')
                    testgui = None

            if self._username and self._password and seriennummer and self._websocketaddr and not testgui:
                if testgui_web:
                    logger.info('Verwende Web Verbindung')
                    testgui = testgui_web
                else:
                    logger.debug('Teste Web Verbindungsart')
                    testgui = E3DCWebGui(self._username, self._password, seriennummer)
                    try:
                        result = test_connection(testgui)
                        if not address:
                            address = repr(result['INFO_IP_ADDRESS'])
                        logger.info('Verwende Web Verbindung / Verbindung mit System ' + repr(result['INFO_SERIAL_NUMBER']) + ' / ' + repr(result['INFO_IP_ADDRESS']))
                    except:
                        logger.exception('Fehler beim Aufbau der Web Verbindung')
                        testgui = None

            if not testgui:
                logger.error('Es konnte keine Verbindungsart ermittelt werden')
            else:
                if self._seriennummer != seriennummer:
                    self._seriennummer = seriennummer
                    self.txtConfigSeriennummer.SetValue(seriennummer)

                if self._address != address:
                    self._address = address
                    self.txtIP.SetValue(address)

                self._gui = testgui
        elif self._username and self._password and self._address and self._rscppass and self._connectiontype == 'direkt':
            testgui = E3DCGui(self._username, self._password, self._address, self._rscppass)
            try:
                result = test_connection(testgui)
                self._gui = testgui
                logger.info('Verwende Direkte Verbindung')
            except:
                self._gui = None
        elif self._username and self._password and self._seriennummer and self._websocketaddr and self._connectiontype == 'web':
            testgui = E3DCWebGui(self._username, self._password, self._seriennummer)
            try:
                result = test_connection(testgui)
                self._gui = testgui
                logger.info('Verwende Websocket')
            except:
                self._gui = None
        else:
            self._gui = None

        if not self._gui:
            logger.info('Kein Verbindungstyp kann verwendet werden, es fehlen Verbindungsdaten')

        return self._gui

    def fill_info(self):
        logger.debug('Rufe INFO-Daten ab')
        d = self.gui.get_data(self.gui.getInfo() + self.gui.getUpdateStatus(), True)
        self._data_info = d

        self.txtProductionDate.SetValue(repr(d['INFO_PRODUCTION_DATE']))
        self.txtSerialnumber.SetValue(repr(d['INFO_SERIAL_NUMBER']))
        self.txtSwRelease.SetValue(repr(d['INFO_SW_RELEASE']))
        self.txtA35Serial.SetValue(repr(d['INFO_A35_SERIAL_NUMBER']))
        dd = datetime.datetime.utcfromtimestamp(float(d['INFO_TIME'].data))
        self.txtTime.SetValue(str(dd.strftime(self._time_format)))
        self.cbTimezone.SetValue(repr(d['INFO_TIME_ZONE']))
        dd = datetime.datetime.utcfromtimestamp(float(d['INFO_UTC_TIME'].data))
        self.txtTimeUTC.SetValue(str(dd))
        self.txtUpdateStatus.SetValue(repr(d['UM_UPDATE_STATUS']))
        self.txtIPAdress.SetValue(repr(d['INFO_IP_ADDRESS']))
        self.txtSubnetmask.SetValue(repr(d['INFO_SUBNET_MASK']))
        self.txtMacAddress.SetValue(repr(d['INFO_MAC_ADDRESS']))
        self.txtGateway.SetValue(repr(d['INFO_GATEWAY']))
        self.txtDNSServer.SetValue(repr(d['INFO_DNS']))
        self.chDHCP.SetValue(d['INFO_DHCP_STATUS'].data)
        self.chSRVIsOnline.SetValue(d['SRV_IS_ONLINE'].data)
        self.chSYSReboot.SetValue(d['SYS_IS_SYSTEM_REBOOTING'].data)
        self.txtRSCPUserLevel.SetValue(repr(d['RSCP_USER_LEVEL']))

        logger.debug('Abruf INFO-Daten abgeschlossen')



    def bEMSUploadChangesOnClick( self, event ):
        r = []
        test = 1 if self.chEMSBatteryBeforeCarMode.GetValue() == False else 0
        if test != self._data_ems['EMS_BATTERY_BEFORE_CAR_MODE'].data:
            r.append(RSCPDTO(tag = RSCPTag.EMS_REQ_SET_BATTERY_BEFORE_CAR_MODE, rscp_type=RSCPType.UChar8, data=test))

        test = 0 if self.chEMSBatteryToCarMode.GetValue() == False else 1
        if test != self._data_ems['EMS_BATTERY_TO_CAR_MODE'].data:
            r.append(RSCPDTO(tag = RSCPTag.EMS_REQ_SET_BATTERY_TO_CAR_MODE, rscp_type=RSCPType.UChar8, data=test))

        temp = []

        test = self.chEMSPowerLimitsUsed.GetValue()
        if test != self._data_ems['EMS_GET_POWER_SETTINGS']['EMS_POWER_LIMITS_USED'].data:
            temp.append(RSCPDTO(tag=RSCPTag.EMS_POWER_LIMITS_USED, rscp_type=RSCPType.Bool, data=test))

        test = 1 if self.chEMSPowerSaveEnabled.GetValue() else 0
        if test != self._data_ems['EMS_GET_POWER_SETTINGS']['EMS_POWERSAVE_ENABLED'].data:
            temp.append(RSCPDTO(tag=RSCPTag.EMS_POWERSAVE_ENABLED, rscp_type=RSCPType.UChar8, data=test))

        test = 1 if self.chEMSWeatherRegulatedChargeEnabled.GetValue() else 0
        if test != self._data_ems['EMS_GET_POWER_SETTINGS']['EMS_WEATHER_REGULATED_CHARGE_ENABLED'].data:
            temp.append(RSCPDTO(tag=RSCPTag.EMS_WEATHER_REGULATED_CHARGE_ENABLED, rscp_type=RSCPType.UChar8, data=test))

        test = self.sEMSMaxChargePower.GetValue()
        if test != self._data_ems['EMS_GET_POWER_SETTINGS']['EMS_MAX_CHARGE_POWER'].data:
            temp.append(RSCPDTO(tag=RSCPTag.EMS_MAX_CHARGE_POWER, rscp_type=RSCPType.Uint32, data=test))

        test = self.sEMSMaxDischargePower.GetValue()
        if test != self._data_ems['EMS_GET_POWER_SETTINGS']['EMS_MAX_DISCHARGE_POWER'].data:
            temp.append(RSCPDTO(tag=RSCPTag.EMS_MAX_DISCHARGE_POWER, rscp_type=RSCPType.Uint32, data=test))

        test = self.sEMSMaxDischargeStartPower.GetValue()
        if test != self._data_ems['EMS_GET_POWER_SETTINGS']['EMS_DISCHARGE_START_POWER'].data:
            temp.append(RSCPDTO(tag=RSCPTag.EMS_DISCHARGE_START_POWER, rscp_type=RSCPType.Uint32, data=test))

        if len(temp) > 0:
            r.append(RSCPDTO(tag=RSCPTag.EMS_REQ_SET_POWER_SETTINGS, rscp_type = RSCPType.Container, data=temp))

        days = ['Mo','Di','Mi','Do','Fr','Sa','So']

        for idlePeriod in self._data_ems['EMS_GET_IDLE_PERIODS']['EMS_IDLE_PERIOD']:
            if idlePeriod['EMS_IDLE_PERIOD_TYPE'].data == 0:
                c = 'Charge'
            else:
                c = 'Discharge'

            day = days[int(idlePeriod['EMS_IDLE_PERIOD_DAY'])]
            von = 'tpEMS' + c + day + 'Von'
            bis = 'tpEMS' + c + day + 'Bis'
            ch = 'chEMS' + c + day

            ch_data = self.__getattribute__(ch).GetValue()
            von_data = self.__getattribute__(von).GetValue().split(':')
            if len(von_data) == 2:
                von_data_hour = int(von_data[0])
                von_data_minute = int(von_data[1])
            else:
                raise Exception('Dateneingabe im Feld ' + von + ' falsch')

            self.__getattribute__(bis).SetValue(str(idlePeriod['EMS_IDLE_PERIOD_END']['EMS_IDLE_PERIOD_HOUR'].data).zfill(2) + ':' + str(idlePeriod['EMS_IDLE_PERIOD_END']['EMS_IDLE_PERIOD_MINUTE'].data).zfill(2))

            bis_data = self.__getattribute__(bis).GetValue().split(':')
            if len(bis_data) == 2:
                bis_data_hour = int(bis_data[0])
                bis_data_minute = int(bis_data[1])
            else:
                raise Exception('Dateneingabe im Feld ' + bis + ' falsch')

            if idlePeriod['EMS_IDLE_PERIOD_ACTIVE'].data != ch_data or \
                    idlePeriod['EMS_IDLE_PERIOD_START']['EMS_IDLE_PERIOD_HOUR'].data != von_data_hour or \
                    idlePeriod['EMS_IDLE_PERIOD_START']['EMS_IDLE_PERIOD_MINUTE'].data != von_data_minute or \
                    idlePeriod['EMS_IDLE_PERIOD_END']['EMS_IDLE_PERIOD_HOUR'].data != bis_data_hour or \
                    idlePeriod['EMS_IDLE_PERIOD_END']['EMS_IDLE_PERIOD_MINUTE'].data != bis_data_minute:
                res = self.gui.setIdlePeriod(type = idlePeriod['EMS_IDLE_PERIOD_TYPE'].data,
                                       active = ch_data,
                                       day = idlePeriod['EMS_IDLE_PERIOD_DAY'].data,
                                       start = str(von_data_hour).zfill(2) + ':' + str(von_data_minute).zfill(2),
                                       end = str(bis_data_hour).zfill(2) + ':' + str(bis_data_minute).zfill(2))
                r += res

        if len(r) > 0:
            try:
                res = self.gui.get_data(r, True)
                wx.MessageBox('Übertragung abgeschlossen')
            except:
                traceback.print_exc()
                wx.MessageBox('Übertragung fehlgeschlagen')
        else:
            res = wx.MessageBox('Es wurden keine Änderungen gemacht, aktuelle Einstellungen trotzdem übertragen?', 'Ladeeinstellungen speichern', wx.YES_NO)
            if res == wx.YES:
                test = 1 if self.chEMSBatteryBeforeCarMode.GetValue() == False else 0
                r.append(RSCPDTO(tag=RSCPTag.EMS_REQ_SET_BATTERY_BEFORE_CAR_MODE, rscp_type=RSCPType.UChar8, data=test))

                test = 0 if self.chEMSBatteryToCarMode.GetValue() == False else 1
                r.append(RSCPDTO(tag=RSCPTag.EMS_REQ_SET_BATTERY_TO_CAR_MODE, rscp_type=RSCPType.UChar8, data=test))

                temp = []

                test = self.chEMSPowerLimitsUsed.GetValue()
                temp.append(RSCPDTO(tag=RSCPTag.EMS_POWER_LIMITS_USED, rscp_type=RSCPType.Bool, data=test))

                test = 1 if self.chEMSPowerSaveEnabled.GetValue() else 0
                temp.append(RSCPDTO(tag=RSCPTag.EMS_POWERSAVE_ENABLED, rscp_type=RSCPType.UChar8, data=test))

                test = 1 if self.chEMSWeatherRegulatedChargeEnabled.GetValue() else 0
                temp.append(RSCPDTO(tag=RSCPTag.EMS_WEATHER_REGULATED_CHARGE_ENABLED, rscp_type=RSCPType.UChar8, data=test))

                test = self.sEMSMaxChargePower.GetValue()
                temp.append(RSCPDTO(tag=RSCPTag.EMS_MAX_CHARGE_POWER, rscp_type=RSCPType.Uint32, data=test))

                test = self.sEMSMaxDischargePower.GetValue()
                temp.append(RSCPDTO(tag=RSCPTag.EMS_MAX_DISCHARGE_POWER, rscp_type=RSCPType.Uint32, data=test))

                test = self.sEMSMaxDischargeStartPower.GetValue()
                temp.append(RSCPDTO(tag=RSCPTag.EMS_DISCHARGE_START_POWER, rscp_type=RSCPType.Uint32, data=test))

                r.append(RSCPDTO(tag=RSCPTag.EMS_REQ_SET_POWER_SETTINGS, rscp_type=RSCPType.Container, data=temp))

                try:
                    res = self.gui.get_data(r, True)
                    wx.MessageBox('Übertragung abgeschlossen')
                except:
                    traceback.print_exc()
                    wx.MessageBox('Übertragung fehlgeschlagen')
        self.pMainChanged()

    def fill_wb(self):
        self.cbWallbox.Clear()
        self._data_wb = []
        logger.debug('Rufe WB-Daten ab')
        if self.debug:
            from e3dc._rscp_utils import RSCPUtils
            import binascii
            r = RSCPUtils()
        try:
            if self.debug:
                t = 'e3dc0011ff41805f00000000a833bf1912001c10840e0e0b000100040e06040000000000bac1b748'
                bin = binascii.unhexlify(t)
                data = r.decode_data(bin)
            else:
                data = self.gui.get_data(self.gui.getWBCount(), True)
            if data.type == RSCPType.Error:
                raise RSCPCommunicationError('Error bei WB-Abruf', logger)
        except RSCPCommunicationError:
            logger.debug('Keine Wallbox vorhanden')
            self.cbWallbox.Append('keine Wallbox vorhanden')
            return False

        for index in data:
            logger.debug('Rufe Daten für Wallbox #' + str(index.data) + ' ab')
            if self.debug:
                t = 'e3dc0011ff41805f00000000d006f01ac0020000840e0eb9020100040e05020000000400800e030100000100800e07040083b600000200800e070400e5b300000300800e05020004000400800e030100000500800e030100000600800e030100900700800e030100000800800e030100000900800e030100000a00800e05020000000b00800e030100000000860e0e18000100860e010100010200860e010100010300860e010100000c00800e0b0800000000a0467095400d00800e0b08000000006055b90f400e00800e0b08000000006055b90f400f00800e030100071100800e030100001200800e0b0800e801068d6e137d401300800e0b0800aa58ab3dcf79fc3f1400800e0b0800aa58ab3dcf79fc3f1500800e070400000000001600800e030100002900800e0e18003000800e010100013100800e010100013200800e010100001700800e0301000a1800800e070400000000001900800e070400102700001a00800e05020000001b00800e030100061c00800e030100e61d00800e030100001e00800e030100001f00800e05020000002000800e05020000002100800e05020000002200800e030100002300800e030100002400800e030100432500800e030100142600800e030100002700800e010100012800800e010100014000800e0b080000000000000000004200800e0d0c004561737920436f6e6e6563740010040eff0400070000001110840e0e1a001120040e060400080000001020040e1008006405e5b3000004001210840e0e1a001120040e060400080000001020040e10080000009e02000000001310840e0e1a001120040e060400080000001020040e100800640583b6000000041410840e0e1a001120040e060400080000001020040e1008000401b006000000001b10840e0e1a001120040e060400080000001020040e10080000000600000000001a10840e0e1a001120040e060400080000001020040e1008000000000000000000020d6f'
                bin = binascii.unhexlify(t)
                d = r.decode_data(bin)
            else:
                d = self.gui.get_data(self.gui.getWB(index=index.data), True)

            self._data_wb.append(d)
            self.cbWallbox.Append('WB #' + str(index.data))

        logger.debug('Abruf WB-Daten abgeschlossen')

    def fill_mbs(self):
        logger.debug('Rufe Modbus-Daten ab')
        d = self.gui.get_data(self.gui.getModbus(), True)
        self._data_mbs = d
        self.cbMBSProtokoll.Clear()

        self._mbsSettings = {}

        self.chMBSEnabled.SetValue(d['MBS_MODBUS_ENABLED'].data)
        if d['MBS_MODBUS_CONNECTORS']:
            for mbs in d['MBS_MODBUS_CONNECTORS']:
                if mbs.name == 'MBS_MODBUS_CONNECTOR_CONTAINER':
                    self.txtMBSName.SetValue(mbs['MBS_MODBUS_CONNECTOR_NAME'].data)
                    self.txtMBSID.SetValue(repr(mbs['MBS_MODBUS_CONNECTOR_ID']))

                    for setup in mbs['MBS_MODBUS_CONNECTOR_SETUP']:
                        if setup:
                            if setup['MBS_MODBUS_SETUP_NAME'].data == 'Protocol':
                                for value in setup['MBS_MODBUS_SETUP_VALUES']:
                                    self.cbMBSProtokoll.Append(value.data)
                                self.cbMBSProtokoll.SetValue(setup['MBS_MODBUS_SETUP_VALUE'].data)
                            elif setup['MBS_MODBUS_SETUP_NAME'].data == 'Device':
                                self.txtMBSDevice.SetValue(setup['MBS_MODBUS_SETUP_VALUE'].data)
                            elif setup['MBS_MODBUS_SETUP_NAME'].data == 'Port':
                                self.txtMBSPort.SetValue(setup['MBS_MODBUS_SETUP_VALUE'].data)

                            self._mbsSettings[setup['MBS_MODBUS_SETUP_NAME'].data] = setup['MBS_MODBUS_SETUP_VALUE']


        logger.debug('Abruf Modbus-Daten abgeschlossen')

    def set_wb_enabled(self):
        def set_wb_enableddisabled(value):
            self.bWBSave.Enable(value)
            self.chWB1PH.Enable(value)
            self.chWBSunmode.Enable(value)
            self.sWBLadestrom.Enable(value)
            self.bWBStopLoading.Enable(value)

        if len(self._data_wb) > 0:
            set_wb_enableddisabled(True)
        else:
            set_wb_enableddisabled(False)

        if self.txtWBMode.GetValue() == 'LOADING':
            self.bWBStopLoading.SetLabel('Ladevorgang abbrechen')
        else:
            self.bWBStopLoading.SetLabel('Ladevorgang starten')

    def bWBStopLoadingClick( self, event ):
        index = self.cbWallbox.GetSelection()
        if len(self._data_wb) > index:
            res = wx.MessageBox('Soll der Ladevorgang in WB#' + str(index) + ' wirklich abgebrochen werden?', 'Wallbox Ladevorgang abbrechen', wx.YES_NO | wx.ICON_QUESTION)
            if res == wx.YES:
                try:
                    ba = bytearray(6)
                    ba[4] = 1
                    req_data = RSCPDTO(tag=RSCPTag.WB_REQ_DATA, rscp_type=RSCPType.Container)
                    req_data += RSCPDTO(tag=RSCPTag.WB_INDEX, rscp_type=RSCPType.UChar8, data=index)
                    req_data += RSCPDTO(tag=RSCPTag.WB_REQ_SET_EXTERN, rscp_type=RSCPType.Container, data=
                    [RSCPDTO(tag=RSCPTag.WB_EXTERN_DATA, rscp_type=RSCPType.ByteArray, data=ba),
                     RSCPDTO(tag=RSCPTag.WB_EXTERN_DATA_LEN, rscp_type=RSCPType.UChar8, data=6)]
                                        )

                    print(req_data)
                    res = self.gui.get_data([req_data], True)
                    print(res)
                    wx.MessageBox('Übertragung durchgeführt')
                except:
                    traceback.print_exc()
                    wx.MessageBox('Übertragung fehlgeschlagen')
        else:
            wx.MessageBox('Wallbox #' + str(index) + ' existiert nicht, dieser Fehler sollte nicht auftreten!')
            logger.error('Wallbox #' + str(index) + ' existiert nicht!')

        self.pMainChanged()

    def fill_wb_index(self, index):
        if len(self._data_wb) > index:
            logger.debug('Stelle Daten der Wallbox #' + str(index) + ' dar')
            d = self._data_wb[index]

            index = d['WB_INDEX'].data

            self.txtWBStatus.SetValue(repr(d['WB_STATUS']))
            self.txtWBEnergyAll.SetValue(repr(d['WB_ENERGY_ALL']) + ' Wh')
            self.txtWBEnergySolar.SetValue(repr(d['WB_ENERGY_SOLAR']) + ' Wh')
            self.txtWBSOC.SetValue(repr(d['WB_SOC']))
            self.txtWBErrorCode.SetValue(repr(d['WB_ERROR_CODE']))
            self.txtWBDeviceName.SetValue(repr(d['WB_DEVICE_NAME']))
            self.txtWBMode.SetValue(repr(d['WB_MODE']))

            self.gWBData.SetCellValue(0,0,str(round(d['WB_PM_POWER_L1'],3)) + ' W')
            self.gWBData.SetCellValue(0,1,str(round(d['WB_PM_POWER_L2'],3)) + ' W')
            self.gWBData.SetCellValue(0,2,str(round(d['WB_PM_POWER_L3'],3)) + ' W')
            self.gWBData.SetCellValue(0,3,str(round(d['WB_PM_POWER_L1'].data + d['WB_PM_POWER_L2'].data + d['WB_PM_POWER_L3'].data,3)) + ' W')

            self.gWBData.SetCellValue(1,0,str(round(d['WB_PM_ENERGY_L1'],3)) + ' Wh')
            self.gWBData.SetCellValue(1,1,str(round(d['WB_PM_ENERGY_L2'],3)) + ' Wh')
            self.gWBData.SetCellValue(1,2,str(round(d['WB_PM_ENERGY_L3'],3)) + ' Wh')
            self.gWBData.SetCellValue(1,3,str(round(d['WB_PM_ENERGY_L1'].data + d['WB_PM_ENERGY_L2'].data + d['WB_PM_ENERGY_L3'].data,3)) + ' Wh')

            self.chWBDeviceConnected.SetValue(d['WB_DEVICE_STATE']['WB_DEVICE_CONNECTED'].data)
            self.chWBDeviceInService.SetValue(d['WB_DEVICE_STATE']['WB_DEVICE_IN_SERVICE'].data)
            self.chWBDeviceWorking.SetValue(d['WB_DEVICE_STATE']['WB_DEVICE_WORKING'].data)

            alg_data = d['WB_EXTERN_DATA_ALG']['WB_EXTERN_DATA'].data
            sb = alg_data[2]
            self.chWBSunmode.SetValue((sb & 128) == 128)

            if (sb & 64) == 64:
                logger.debug('WB charging canceled True')
            else:
                logger.debug('WB charging canceled False')
            if (sb & 32) == 32:
                logger.debug('WB charging active True')
            else:
                logger.debug('WB charging active False')

            self.chWB1PH.SetValue(alg_data[1] == 1)

            def get_load(data):
                return data[1] << 8 | data[0]

            sunload = get_load(d['WB_EXTERN_DATA_SUN']['WB_EXTERN_DATA'].data)
            self.txtWBSun.SetValue(str(sunload) + ' W')

            netload = get_load(d['WB_EXTERN_DATA_NET']['WB_EXTERN_DATA'].data)
            self.txtWBNet.SetValue(str(netload) + ' W')

            self.txtWBLadeleistung.SetValue(str(sunload+netload) + ' W')


            self.sWBLadestrom.SetValue(d['WB_RSP_PARAM_1']['WB_EXTERN_DATA'].data[2])

            logger.debug('Darstellung Wallbox abgeschlossen')
        else:
            logger.debug('Daten für Wallbox #' + str(index) + ' existieren nicht, Darstellung abgebrochen')

            # Bedeutung???
            #WB_REQ_SET_MODE = 0x0E000030
            #WB_REQ_SET_EXTERN = 0x0E041010
            #WB_REQ_SET_BAT_CAPACITY = 0x0E041015
            #WB_REQ_SET_ENERGY_ALL = 0x0E041016
            #WB_REQ_SET_ENERGY_SOLAR = 0x0E041017
            #WB_REQ_SET_PARAM_1 = 0x0E041018
            #WB_REQ_SET_PARAM_2 = 0x0E041019
            #WB_REQ_SET_PW = 0x0E041020
            # WB_REQ_SET_DEVICE_NAME

    def bWBSaveOnClick( self, event ):
        index = self.cbWallbox.GetSelection()

        data = self._data_wb[index]

        index = data['WB_INDEX'].data

        r = []
        test_changed = self.chWBSunmode.GetValue()
        test_orig = (data['WB_EXTERN_DATA_ALG']['WB_EXTERN_DATA'].data[2] & 128) == 128
        if test_changed != test_orig:
            ba = bytearray(6)
            ba[0] = 1 if test_changed else 2
            req_data  = RSCPDTO(tag = RSCPTag.WB_REQ_DATA, rscp_type = RSCPType.Container)
            req_data += RSCPDTO(tag = RSCPTag.WB_INDEX, rscp_type = RSCPType.UChar8, data = index)
            req_data += RSCPDTO(tag = RSCPTag.WB_REQ_SET_EXTERN, rscp_type = RSCPType.Container, data =
                                [RSCPDTO(tag = RSCPTag.WB_EXTERN_DATA, rscp_type = RSCPType.ByteArray, data = ba),
                                 RSCPDTO(tag = RSCPTag.WB_EXTERN_DATA_LEN, rscp_type = RSCPType.UChar8, data = 6)]
                                )
            r.append(req_data)

        test_changed = self.chWB1PH.GetValue()
        test_orig = (data['WB_EXTERN_DATA_ALG']['WB_EXTERN_DATA'].data[1] == 1)
        if test_changed != test_orig:
            ba = bytearray(6)
            ba[3] = 1 if test_changed else 3
            req_data  = RSCPDTO(tag = RSCPTag.WB_REQ_DATA, rscp_type = RSCPType.Container)
            req_data += RSCPDTO(tag = RSCPTag.WB_INDEX, rscp_type = RSCPType.UChar8, data = index)
            req_data += RSCPDTO(tag = RSCPTag.WB_REQ_SET_EXTERN, rscp_type = RSCPType.Container, data =
                                [RSCPDTO(tag = RSCPTag.WB_EXTERN_DATA, rscp_type = RSCPType.ByteArray, data = ba),
                                 RSCPDTO(tag = RSCPTag.WB_EXTERN_DATA_LEN, rscp_type = RSCPType.UChar8, data = 6)]
                                )
            r.append(req_data)

        test_changed = self.sWBLadestrom.GetValue()
        param_1 = bytearray(data['WB_RSP_PARAM_1']['WB_EXTERN_DATA'].data)
        if test_changed != param_1[2]:

            param_1[2] = test_changed
            req_data  = RSCPDTO(tag = RSCPTag.WB_REQ_DATA, rscp_type = RSCPType.Container)
            req_data += RSCPDTO(tag = RSCPTag.WB_INDEX, rscp_type = RSCPType.UChar8, data = index)
            req_data += RSCPDTO(tag = RSCPTag.WB_REQ_SET_PARAM_1, rscp_type = RSCPType.Container, data =
                                [RSCPDTO(tag = RSCPTag.WB_EXTERN_DATA, rscp_type = RSCPType.ByteArray, data = param_1),
                                 RSCPDTO(tag = RSCPTag.WB_EXTERN_DATA_LEN, rscp_type = RSCPType.UChar8, data = 6)]
                                )
            r.append(req_data)

        for i in r:
            print(i)

        if len(r) > 0:
            try:
                res = self.gui.get_data(r, True)
                print(res)
                wx.MessageBox('Übertragung abgeschlossen')
            except:
                traceback.print_exc()
                wx.MessageBox('Übertragung fehlgeschlagen')
        else:
            res = wx.MessageBox('Es wurden keine Änderungen gemacht, aktuelle Einstellungen trotzdem übertragen?', 'Wallbox speichern', wx.YES_NO)
            if res == wx.YES:
                r = []
                test_changed = self.chWBSunmode.GetValue()
                ba = bytearray(6)
                ba[0] = 1 if test_changed else 2
                req_data = RSCPDTO(tag=RSCPTag.WB_REQ_DATA, rscp_type=RSCPType.Container)
                req_data += RSCPDTO(tag=RSCPTag.WB_INDEX, rscp_type=RSCPType.UChar8, data=index)
                req_data += RSCPDTO(tag=RSCPTag.WB_REQ_SET_EXTERN, rscp_type=RSCPType.Container, data=
                [RSCPDTO(tag=RSCPTag.WB_EXTERN_DATA, rscp_type=RSCPType.ByteArray, data=ba),
                 RSCPDTO(tag=RSCPTag.WB_EXTERN_DATA_LEN, rscp_type=RSCPType.UChar8, data=6)]
                                    )
                r.append(req_data)

                test_changed = self.chWB1PH.GetValue()
                ba = bytearray(6)
                ba[3] = 1 if test_changed else 3
                req_data = RSCPDTO(tag=RSCPTag.WB_REQ_DATA, rscp_type=RSCPType.Container)
                req_data += RSCPDTO(tag=RSCPTag.WB_INDEX, rscp_type=RSCPType.UChar8, data=index)
                req_data += RSCPDTO(tag=RSCPTag.WB_REQ_SET_EXTERN, rscp_type=RSCPType.Container, data=
                [RSCPDTO(tag=RSCPTag.WB_EXTERN_DATA, rscp_type=RSCPType.ByteArray, data=ba),
                 RSCPDTO(tag=RSCPTag.WB_EXTERN_DATA_LEN, rscp_type=RSCPType.UChar8, data=6)]
                                    )
                r.append(req_data)

                test_changed = self.sWBLadestrom.GetValue()
                param_1[2] = test_changed
                req_data = RSCPDTO(tag=RSCPTag.WB_REQ_DATA, rscp_type=RSCPType.Container)
                req_data += RSCPDTO(tag=RSCPTag.WB_INDEX, rscp_type=RSCPType.UChar8, data=index)
                req_data += RSCPDTO(tag=RSCPTag.WB_REQ_SET_PARAM_1, rscp_type=RSCPType.Container, data=
                [RSCPDTO(tag=RSCPTag.WB_EXTERN_DATA, rscp_type=RSCPType.ByteArray, data=param_1),
                 RSCPDTO(tag=RSCPTag.WB_EXTERN_DATA_LEN, rscp_type=RSCPType.UChar8, data=6)]
                                    )
                r.append(req_data)

                for i in r:
                    print(i)

                try:
                    res = self.gui.get_data(r, True)
                    print(res)
                    wx.MessageBox('Übertragung abgeschlossen')
                except:
                    traceback.print_exc()
                    wx.MessageBox('Übertragung fehlgeschlagen')

        self.pMainChanged()

    def fill_ems(self):
        logger.debug('Rufe EMS-Daten ab')
        self._extsrcavailable = 0
        d = self.gui.get_data(self.gui.getEMSData(), True)
        self._data_ems = d

        self.txtEMSPowerPV.SetValue(repr(d['EMS_POWER_PV']) + ' W')
        self.txtEMSPowerHome.SetValue(repr(d['EMS_POWER_HOME']) + ' W')
        self.txtEMSPowerBat.SetValue(repr(d['EMS_POWER_BAT']) + ' W')
        self.txtEMSPowerGrid.SetValue(repr(d['EMS_POWER_GRID']) + ' W')
        self.txtEMSPowerAdd.SetValue(repr(d['EMS_POWER_ADD']) + ' W')
        self.txtEMSAutarkie.SetValue(str(round(d['EMS_AUTARKY'],2)) + ' %')
        self.gEMSAutarkie.SetValue(int(round(d['EMS_AUTARKY'],0)))
        self.txtEMSSelfConsumption.SetValue(str(round(d['EMS_SELF_CONSUMPTION'],2)) + ' %')
        self.gEMSSelfConsumption.SetValue(int(round(d['EMS_SELF_CONSUMPTION'],0)))
        self.txtEMSBatSoc.SetValue(str(round(d['EMS_BAT_SOC'],2)) + ' %')
        self.mgEMSBatSoc.SetValue(round(d['EMS_BAT_SOC'],0))
        self.txtEMSCouplingMode.SetValue(repr(d['EMS_COUPLING_MODE']))
        self.txtEMSMode.SetValue(repr(d['EMS_MODE']))
        if d['EMS_BATTERY_BEFORE_CAR_MODE'].data == 1:
            self.chEMSBatteryBeforeCarMode.SetValue(False)
        else:
            self.chEMSBatteryBeforeCarMode.SetValue(True)
        if d['EMS_BATTERY_TO_CAR_MODE'].data == 1:
            self.chEMSBatteryToCarMode.SetValue(True)
        else:
            self.chEMSBatteryToCarMode.SetValue(False)
        balancedphases = "{0:b}".format(d['EMS_BALANCED_PHASES'].data).replace('1','X ').replace('0','0 ')

        self.txtEMSBalancedPhases.SetValue(balancedphases)
        self.txtEMSExtSrcAvailable.SetValue(repr(d['EMS_EXT_SRC_AVAILABLE']))
        self._extsrcavailable = d['EMS_EXT_SRC_AVAILABLE'].data
        self.txtEMSInstalledPeakPower.SetValue(repr(d['EMS_INSTALLED_PEAK_POWER']) + ' Wp')
        self.txtEMSDerateAtPercent.SetValue(str(round(d['EMS_DERATE_AT_PERCENT_VALUE'],3)*100) + ' %')
        self.txtEMSDerateAtPower.SetValue(repr(d['EMS_DERATE_AT_POWER_VALUE']) + ' W')
        self.txtEMSUsedChargeLimit.SetValue(repr(d['EMS_USED_CHARGE_LIMIT']) + ' W')
        self.txtEMSUserChargeLimit.SetValue(repr(d['EMS_USER_CHARGE_LIMIT']) + ' W')
        self.txtEMSBatChargeLimit.SetValue(repr(d['EMS_BAT_CHARGE_LIMIT']) + ' W')
        self.txtEMSDCDCChargeLimit.SetValue(repr(d['EMS_DCDC_CHARGE_LIMIT']) + ' W')
        self.txtEMSRemainingBatChargePower.SetValue(repr(d['EMS_REMAINING_BAT_CHARGE_POWER']) + ' W')
        self.txtEMSUsedDischargeLimit.SetValue(repr(d['EMS_USED_DISCHARGE_LIMIT']) + ' W')
        self.txtEMSUserDischargeLimit.SetValue(repr(d['EMS_USER_DISCHARGE_LIMIT']) + ' W')
        self.txtEMSBatDischargeLimit.SetValue(repr(d['EMS_BAT_DISCHARGE_LIMIT']) + ' W')
        self.txtEMSDCDCDischargeLimit.SetValue(repr(d['EMS_DCDC_DISCHARGE_LIMIT']) + ' W')
        self.txtEMSRemainingBatDischargePower.SetValue(repr(d['EMS_REMAINING_BAT_DISCHARGE_POWER']) + ' W')
        self.txtEMSEmergencyPowerStatus.SetValue(repr(d['EMS_EMERGENCY_POWER_STATUS']))
        self.chEMSPowerLimitsUsed.SetValue(d['EMS_GET_POWER_SETTINGS']['EMS_POWER_LIMITS_USED'].data)
        self.chEMSPowerSaveEnabled.SetValue(d['EMS_GET_POWER_SETTINGS']['EMS_POWERSAVE_ENABLED'].data)
        self.chEMSWeatherRegulatedChargeEnabled.SetValue(d['EMS_GET_POWER_SETTINGS']['EMS_WEATHER_REGULATED_CHARGE_ENABLED'].data)
        self.txtEMSStatus.SetValue(repr(d['EMS_STATUS']))
        eptest = d['EMS_EMERGENCYPOWER_TEST_STATUS']['EMS_EPTEST_RUNNING'].data
        if eptest:
            self.bEMSEPTest.Enable(False)
        else:
            self.bEMSEPTest.Enable(True)
        self.chEMSEPTestRunning.SetValue(eptest)
        self.txtEMSEPTestCounter.SetValue(repr(d['EMS_EMERGENCYPOWER_TEST_STATUS']['EMS_EPTEST_START_COUNTER']))
        self.txtEMSEPTestTimestamp.SetValue(repr(d['EMS_EMERGENCYPOWER_TEST_STATUS']['EMS_EPTEST_NEXT_TESTSTART']))

        self.txtEMSPowerWBAll.SetValue(repr(d['EMS_POWER_WB_ALL']) + ' W')
        self.txtEMSPowerWBSolar.SetValue(repr(d['EMS_POWER_WB_SOLAR']) + ' W')
        self.chEMSAlive.SetValue(d['EMS_ALIVE'].data)

        manChargeActive = d['EMS_GET_MANUAL_CHARGE']['EMS_MANUAL_CHARGE_ACTIVE'].data

        self.chEMSGetManualCharge.SetValue(manChargeActive)
        if manChargeActive:
            self.bEMSManualChargeStart.Enable(False)
        else:
            self.bEMSManualChargeStart.Enable(True)

        startcounter = d['EMS_GET_MANUAL_CHARGE']['EMS_MANUAL_CHARGE_START_COUNTER'].data/1000
        dd = datetime.datetime.fromtimestamp(startcounter)
        self.txtEMSManualChargeStartCounter.SetValue(dd.strftime(self._time_format))

        self.txtEMSManualChargeEnergyCounter.SetValue(str(round(d['EMS_GET_MANUAL_CHARGE']['EMS_MANUAL_CHARGE_ENERGY_COUNTER'],5)) + ' kWh') # TODO: Einheit anfügen

        laststart = d['EMS_GET_MANUAL_CHARGE']['EMS_MANUAL_CHARGE_LASTSTART'].data
        dd = datetime.datetime.fromtimestamp(laststart)
        self.txtEMSManualChargeLaststart.SetValue(dd.strftime(self._time_format))

        for sysspec in d['EMS_GET_SYS_SPECS']['EMS_SYS_SPEC']:
            if sysspec['EMS_SYS_SPEC_NAME'].data == 'hybridModeSupported':
                self.txtEMSHybridModeSupported.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'] .data== 'installedBatteryCapacity':
                self.txtEMSInstalledBatteryCapacity.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'maxAcPower':
                self.txtEMSMaxAcPower.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'maxBatChargePower':
                self.txtEMSMaxBatChargePower.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'maxBatDischargPower':
                self.txtEMSMaxBatDischargePower.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'maxChargePower':
                self.txtEMSMaxChargePowerSys.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'maxDischargePower':
                self.txtEMSMaxDischargePowerSys.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'maxFbcChargePower':
                self.txtEMSMaxFbcDischargePower.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'maxPvPower':
                self.txtEMSMaxPVPower.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'maxStartChargePower':
                self.txtEMSMaxStartChargePower.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
                maxStartChargePower = int(sysspec['EMS_SYS_SPEC_VALUE_INT'])
                self.sEMSMaxChargePower.Max = maxStartChargePower
                self.sEMSMaxDischargeStartPower.Max = maxStartChargePower
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'maxStartDischargePower':
                self.txtEMSMaxStartDischargePower.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
                maxStartDischargePower = int(sysspec['EMS_SYS_SPEC_VALUE_INT'])
                self.sEMSMaxDischargePower.Max = maxStartDischargePower
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'minStartChargePower':
                self.txtEMSMinStartChargePower.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
                minStartChargePower = int(sysspec['EMS_SYS_SPEC_VALUE_INT'])
                self.sEMSMaxChargePower.Min = minStartChargePower
                self.sEMSMaxDischargeStartPower.Min = minStartChargePower
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'minStartDischargePower':
                self.txtEMSMinStartDischargePower.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
                minStartDischargePower = int(sysspec['EMS_SYS_SPEC_VALUE_INT'])
                self.sEMSMaxDischargePower.Min = minStartDischargePower
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'recommendedMinChargeLimit':
                self.txtEMSRecommendedMinChargeLimit.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'recommendedMinDischargeLimit':
                self.txtEMSRecommendedMinDischargeLimit.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'startChargeDefault':
                self.txtEMSstartChargeDefault.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))
            elif sysspec['EMS_SYS_SPEC_NAME'].data == 'startDischargeDefault':
                self.txtEMSstartDischargeDefault.SetValue(repr(sysspec['EMS_SYS_SPEC_VALUE_INT']))

        self.sEMSMaxChargePower.SetValue(d['EMS_GET_POWER_SETTINGS']['EMS_MAX_CHARGE_POWER'].data)
        self.txtEMSMaxChargePower.SetValue(repr(d['EMS_GET_POWER_SETTINGS']['EMS_MAX_CHARGE_POWER']) + ' W')
        self.sEMSMaxDischargePower.SetValue(d['EMS_GET_POWER_SETTINGS']['EMS_MAX_DISCHARGE_POWER'].data)
        self.txtEMSMaxDischargePower.SetValue(repr(d['EMS_GET_POWER_SETTINGS']['EMS_MAX_DISCHARGE_POWER']) + ' W')
        self.sEMSMaxDischargeStartPower.SetValue(d['EMS_GET_POWER_SETTINGS']['EMS_DISCHARGE_START_POWER'].data)
        self.txtEMSMaxDischargeStartPower.SetValue(repr(d['EMS_GET_POWER_SETTINGS']['EMS_DISCHARGE_START_POWER']) + ' W')

        self.chEPIsland.SetValue(d['EP_IS_ISLAND_GRID'].data)
        self.chEPReadyForSwitch.SetValue(d['EP_IS_READY_FOR_SWITCH'].data)
        self.chEPISGridConnected.SetValue(d['EP_IS_GRID_CONNECTED'].data)
        self.chEPPossible.SetValue(d['EP_IS_POSSIBLE'].data)
        self.chEPInvalid.SetValue(d['EP_IS_INVALID_STATE'].data)

        days = ['Mo','Di','Mi','Do','Fr','Sa','So']

        for idlePeriod in d['EMS_GET_IDLE_PERIODS']['EMS_IDLE_PERIOD']:
            if idlePeriod['EMS_IDLE_PERIOD_TYPE'].data == 0:
                c = 'Charge'
            else:
                c = 'Discharge'

            day = days[int(idlePeriod['EMS_IDLE_PERIOD_DAY'])]
            von = 'tpEMS' + c + day + 'Von'
            bis = 'tpEMS' + c + day + 'Bis'
            ch = 'chEMS' + c + day

            self.__getattribute__(ch).SetValue(idlePeriod['EMS_IDLE_PERIOD_ACTIVE'].data)
            self.__getattribute__(von).SetValue(str(idlePeriod['EMS_IDLE_PERIOD_START']['EMS_IDLE_PERIOD_HOUR'].data).zfill(2) + ':' + str(idlePeriod['EMS_IDLE_PERIOD_START']['EMS_IDLE_PERIOD_MINUTE'].data).zfill(2))
            self.__getattribute__(bis).SetValue(str(idlePeriod['EMS_IDLE_PERIOD_END']['EMS_IDLE_PERIOD_HOUR'].data).zfill(2) + ':' + str(idlePeriod['EMS_IDLE_PERIOD_END']['EMS_IDLE_PERIOD_MINUTE'].data).zfill(2))
        logger.debug('Abruf EMS-Daten abgeschlossen')

    def sEMSMaxChargePowerOnScroll(self, event):
        self.txtEMSMaxChargePower.SetValue(str(self.sEMSMaxChargePower.GetValue()) + ' W')

    def sEMSMaxDischargePowerOnScroll(self, event):
        self.txtEMSMaxDischargePower.SetValue(str(self.sEMSMaxDischargePower.GetValue()) + ' W')

    def sEMSMaxDischargeStartPowerOnScroll(self, event):
        self.txtEMSMaxDischargeStartPower.SetValue(str(self.sEMSMaxDischargeStartPower.GetValue()) + ' W')

    def fill_pm(self):
        logger.debug('Rufe PM-Daten ab')
        def get_einheit(value):
            if abs(value) > 10000:
                if abs(value) > 10000000:
                    return value / 1000000, 'MWh'
                else:
                    return value / 1000, 'kWh'
            else:
                return value, 'Wh'

        if self._extsrcavailable >= 0:
            indexes = range(0,8)
        else:
            indexes = None

        self.gPM.DeleteCols(numCols=self.gPM.GetNumberCols())
        self._data_pm = {}

        for index in indexes:
            try:
                d = self.gui.get_data(self.gui.getPMData(pm_index=index), True)

                if 'PM_DEVICE_STATE' not in d or d['PM_DEVICE_STATE'].type != RSCPType.Error:
                    index = d['PM_INDEX'].data
                    self.gPM.AppendCols(1)
                    curcol = self.gPM.GetNumberCols() - 1
                    self.gPM.SetColLabelValue(index, 'PM #' + str(index))
                    self._data_pm[index] = d
                    self.gPM.SetCellValue(0, curcol, str(round(d['PM_POWER_L1'], 3)) + ' W')
                    self.gPM.SetCellValue(1, curcol, str(round(d['PM_POWER_L2'], 3)) + ' W')
                    self.gPM.SetCellValue(2, curcol, str(round(d['PM_POWER_L3'], 3)) + ' W')
                    self.gPM.SetCellValue(3, curcol, str(round(d['PM_POWER_L1'].data+d['PM_POWER_L2'].data+d['PM_POWER_L3'].data, 3)) + ' W')
                    self.gPM.SetCellValue(4, curcol, str(round(d['PM_VOLTAGE_L1'], 3)) + ' V')
                    self.gPM.SetCellValue(5, curcol, str(round(d['PM_VOLTAGE_L2'], 3)) + ' V')
                    self.gPM.SetCellValue(6, curcol, str(round(d['PM_VOLTAGE_L3'], 3)) + ' V')

                    energy, einheit = get_einheit(d['PM_ENERGY_L1'].data)
                    self.gPM.SetCellValue(7, curcol, str(round(energy, 3)) + ' ' + einheit)
                    energy, einheit = get_einheit(d['PM_ENERGY_L2'].data)
                    self.gPM.SetCellValue(8, curcol, str(round(energy, 3)) + ' ' + einheit)
                    energy, einheit = get_einheit(d['PM_ENERGY_L3'].data)
                    self.gPM.SetCellValue(9, curcol, str(round(energy, 3)) + ' ' + einheit)
                    energy, einheit = get_einheit(d['PM_ENERGY_L1'].data+d['PM_ENERGY_L2'].data+d['PM_ENERGY_L3'].data)
                    self.gPM.SetCellValue(10, curcol, str(round(energy, 3)) + ' ' + einheit)

                    self.gPM.SetCellValue(11, curcol, repr(d['PM_FIRMWARE_VERSION']))
                    self.gPM.SetCellValue(12, curcol, repr(d['PM_ACTIVE_PHASES']))
                    self.gPM.SetCellValue(13, curcol, repr(d['PM_MODE']))
                    self.gPM.SetCellValue(14, curcol, repr(d['PM_ERROR_CODE']))
                    self.gPM.SetCellValue(15, curcol, repr(d['PM_TYPE']))
                    self.gPM.SetCellValue(16, curcol, repr(d['PM_DEVICE_ID']))
                    self.gPM.SetCellValue(17, curcol, repr(d['PM_IS_CAN_SILENCE']))
                    self.gPM.SetCellValue(28, curcol, repr(d['PM_DEVICE_STATE']['PM_DEVICE_CONNECTED']))
                    self.gPM.SetCellValue(29, curcol, repr(d['PM_DEVICE_STATE']['PM_DEVICE_WORKING']))
                    self.gPM.SetCellValue(30, curcol, repr(d['PM_DEVICE_STATE']['PM_DEVICE_IN_SERVICE']))

                    if 'PM_COMM_STATE' in d:
                        d = d['PM_COMM_STATE']
                        self.gPM.SetCellValue(18, curcol, repr(d['PM_CS_START_TIME']))
                        self.gPM.SetCellValue(19, curcol, repr(d['PM_CS_LAST_TIME']))
                        self.gPM.SetCellValue(20, curcol, repr(d['PM_CS_SUCC_FRAMES_ALL']))
                        self.gPM.SetCellValue(21, curcol, repr(d['PM_CS_SUCC_FRAMES_100']))
                        self.gPM.SetCellValue(22, curcol, repr(d['PM_CS_EXP_FRAMES_ALL']))
                        self.gPM.SetCellValue(23, curcol, repr(d['PM_CS_EXP_FRAMES_100']))
                        self.gPM.SetCellValue(24, curcol, repr(d['PM_CS_ERR_FRAMES_ALL']))
                        self.gPM.SetCellValue(25, curcol, repr(d['PM_CS_ERR_FRAMES_100']))
                        self.gPM.SetCellValue(26, curcol, repr(d['PM_CS_UNK_FRAMES']))
                        self.gPM.SetCellValue(27, curcol, repr(d['PM_CS_ERR_FRAME']))

                    logger.info('PM #' + str(index) + ' erfolgreich abgerufen')
            except:
                logger.exception('PM #' + str(index) + ' konnte nicht abgerufen werden.')

        self.gPM.AutoSize()
        logger.debug('Abruf PM-Daten abgeschlossen')

    def fill_dcdc(self):
        logger.debug('Rufe DCDC-Daten ab')
        self._data_dcdc = []
        for index in [0,1,2,3]:
            try:
                d = self.gui.get_data(self.gui.getDCDCData(dcdc_indexes=[index]), True)
                self._data_dcdc.append(d)

                index = int(d['DCDC_INDEX'])

                self.gDCDC.SetCellValue(0,index, str(round(d['DCDC_I_BAT'].data,5)) + ' A')
                self.gDCDC.SetCellValue(1,index, str(round(d['DCDC_U_BAT'].data,2)) + ' V')
                self.gDCDC.SetCellValue(2,index, str(round(d['DCDC_P_BAT'].data,2)) + ' W')
                self.gDCDC.SetCellValue(3,index, str(round(d['DCDC_I_DCL'].data,5)) + ' A')
                self.gDCDC.SetCellValue(4,index, str(round(d['DCDC_U_DCL'].data,2)) + ' V')
                self.gDCDC.SetCellValue(5,index, str(round(d['DCDC_P_DCL'].data,2)) + ' W')
                self.gDCDC.SetCellValue(6,index, repr(d['DCDC_FIRMWARE_VERSION']))
                self.gDCDC.SetCellValue(7,index, repr(d['DCDC_FPGA_FIRMWARE']))
                self.gDCDC.SetCellValue(8,index, repr(d['DCDC_SERIAL_NUMBER']))
                self.gDCDC.SetCellValue(9,index, str(repr(d['DCDC_BOARD_VERSION'])))
                self.gDCDC.SetCellValue(10,index, repr(d['DCDC_STATUS_AS_STRING']['DCDC_STATE_AS_STRING']))
                self.gDCDC.SetCellValue(11,index, repr(d['DCDC_STATUS_AS_STRING']['DCDC_SUBSTATE_AS_STRING']))
                logger.info('DCDC #' + str(index) + ' wurde erfolgreich abgefragt.')
            except:
                logger.info('DCDC #' + str(index) + ' konnte nicht abgefragt werden.')

        self.gDCDC.AutoSizeColumns()
        logger.debug('Abruf DCDC-Daten abgeschlossen')

    def fill_pvi(self):
        logger.debug('Rufe PVI-Daten ab')
        self._data_pvi = {}
        for index in range(0,4):
            try:
                data = self.gui.get_data(self.gui.getPVIData(pvi_index=index), True)
                self._data_pvi[index] = data
                logger.info('PVI #' + str(index) + ' wurde erfolgreich abgefragt.')
            except:
                logger.exception('PVI #' + str(index) + ' konnte nicht abgefragt werden.')

        self.chPVIIndex.Clear()

        for k in self._data_pvi:
            pvi = self._data_pvi[k]
            self.chPVIIndex.Append('PVI #' + str(pvi['PVI_INDEX'].data))

        logger.debug('Abruf PVI-Daten abgeschlossen')

    def fill_pvi_index(self, index):
        data = self._data_pvi[index]

        self.txtPVISerialNumber.SetValue(repr(data['PVI_SERIAL_NUMBER']))
        self.txtPVIType.SetValue(repr(data['PVI_TYPE']))
        self.txtPVIVersionMain.SetValue(repr(data['PVI_VERSION']['PVI_VERSION_MAIN']))
        self.txtPVIVersionPic.SetValue(repr(data['PVI_VERSION']['PVI_VERSION_PIC']))
        self.txtPVITempCount.SetValue(repr(data['PVI_TEMPERATURE_COUNT']))
        self.chPVIOnGrid.SetValue(data['PVI_ON_GRID'].data)
        self.txtPVIStatus.SetValue(repr(data['PVI_STATE']))
        self.txtPVILastError.SetValue(repr(data['PVI_LAST_ERROR']))
        try:
            self.chPVICosPhiActive.SetValue(data['PVI_COS_PHI']['PVI_COS_PHI_IS_AKTIV'].data)
            self.txtPVICosPhiValue.SetValue(repr(data['PVI_COS_PHI']['PVI_COS_PHI_VALUE']))
            self.txtPVICosPhiExcited.SetValue(repr(data['PVI_COS_PHI']['PVI_COS_PHI_EXCITED']))
        except:
            logger.info('Keine COS-PHI-Werte verfügbar in PVI #' + str(index))
        self.txtPVIVoltageTrTop.SetValue(repr(data['PVI_VOLTAGE_MONITORING']['PVI_VOLTAGE_MONITORING_THRESHOLD_TOP']))
        self.txtPVIVoltageTrBottom.SetValue(repr(data['PVI_VOLTAGE_MONITORING']['PVI_VOLTAGE_MONITORING_THRESHOLD_BOTTOM']))
        self.txtPVIVoltageSlUp.SetValue(repr(data['PVI_VOLTAGE_MONITORING']['PVI_VOLTAGE_MONITORING_SLOPE_UP']))
        self.txtPVIVoltageSlDown.SetValue(repr(data['PVI_VOLTAGE_MONITORING']['PVI_VOLTAGE_MONITORING_SLOPE_DOWN']))
        self.txtPVIPowerMode.SetValue(repr(data['PVI_POWER_MODE']))
        self.txtPVISystemMode.SetValue(repr(data['PVI_SYSTEM_MODE']))
        self.txtPVIMaxTemperature.SetValue(repr(data['PVI_MAX_TEMPERATURE']['PVI_VALUE']))
        self.txtPVIMinTemperature.SetValue(repr(data['PVI_MIN_TEMPERATURE']['PVI_VALUE']))
        self.txtPVIMaxApparentpower.SetValue(repr(data['PVI_AC_MAX_APPARENTPOWER']['PVI_VALUE']))
        self.txtPVIFreqMin.SetValue(repr(data['PVI_FREQUENCY_UNDER_OVER']['PVI_FREQUENCY_UNDER']))
        self.txtPVIMaxFreq.SetValue(repr(data['PVI_FREQUENCY_UNDER_OVER']['PVI_FREQUENCY_OVER']))

        self.chPVIDeviceConnected.SetValue(data['PVI_DEVICE_STATE']['PVI_DEVICE_CONNECTED'].data)
        self.chPVIDeviceWorking.SetValue(data['PVI_DEVICE_STATE']['PVI_DEVICE_WORKING'].data)
        self.chPVIDeviceInService.SetValue(data['PVI_DEVICE_STATE']['PVI_DEVICE_IN_SERVICE'].data)

        values = [{},{},{}]
        for d in data:
            if d.name in ['PVI_AC_POWER','PVI_AC_VOLTAGE','PVI_AC_CURRENT','PVI_AC_APPARENTPOWER','PVI_AC_REACTIVEPOWER','PVI_AC_ENERGY_ALL','PVI_AC_ENERGY_GRID_CONSUMPTION']:
                index = d['PVI_INDEX'].data
                values[index][d.name] = d['PVI_VALUE']
        sum_ac_power = sum_ac_energy_all = sum_ac_energy_grid = 0
        for i in range(0,3):
            self.gPVIAC.SetCellValue(0, i, repr(values[i]['PVI_AC_POWER']) + ' W')
            sum_ac_power+=values[i]['PVI_AC_POWER'].data
            self.gPVIAC.SetCellValue(1, i, repr(round(values[i]['PVI_AC_VOLTAGE'],3)) + ' V')
            self.gPVIAC.SetCellValue(2, i, repr(round(values[i]['PVI_AC_CURRENT'],5)) + ' A')
            self.gPVIAC.SetCellValue(3, i, repr(round(values[i]['PVI_AC_APPARENTPOWER'],3)) + ' VA')
            self.gPVIAC.SetCellValue(4, i, repr(round(values[i]['PVI_AC_REACTIVEPOWER'],3)) + ' VAr')
            self.gPVIAC.SetCellValue(5, i, repr(values[i]['PVI_AC_ENERGY_ALL']) + ' kWh')
            sum_ac_energy_all+=values[i]['PVI_AC_ENERGY_ALL'].data
            self.gPVIAC.SetCellValue(6, i, repr(values[i]['PVI_AC_ENERGY_GRID_CONSUMPTION']) + ' kWh')
            sum_ac_energy_grid+=values[i]['PVI_AC_ENERGY_GRID_CONSUMPTION'].data

        self.gPVIAC.SetCellValue(0,3,str(round(sum_ac_power,3)) + ' W')
        self.gPVIAC.SetCellValue(5,3,str(round(sum_ac_energy_all,3)) + ' kWh')
        self.gPVIAC.SetCellValue(6,3,str(round(sum_ac_energy_grid,3)) + ' kWh')
        self.gPVIAC.AutoSize()

        values = [{},{}]
        for d in data:
            if d.name in ['PVI_DC_POWER','PVI_DC_VOLTAGE','PVI_DC_CURRENT','PVI_DC_STRING_ENERGY_ALL']:
                index = d['PVI_INDEX'].data
                values[index][d.name] = d['PVI_VALUE']
        sum_dc_power = sum_dc_energy = 0
        for i in range(0,2):
            self.gPVIDC.SetCellValue(0, i, repr(values[i]['PVI_DC_POWER']) + ' W')
            sum_dc_power += values[i]['PVI_DC_POWER'].data
            self.gPVIDC.SetCellValue(1, i, repr(values[i]['PVI_DC_VOLTAGE']) + ' V')
            self.gPVIDC.SetCellValue(2, i, str(round(values[i]['PVI_DC_CURRENT'],5)) + ' A')
            self.gPVIDC.SetCellValue(3, i, str(round(values[i]['PVI_DC_STRING_ENERGY_ALL'])/1000) + ' kWh')
            sum_dc_energy += values[i]['PVI_DC_STRING_ENERGY_ALL'].data

        self.gPVIDC.SetCellValue(0,2,str(round(sum_dc_power,3)) + ' W')
        self.gPVIDC.SetCellValue(3,2,str(round(sum_dc_energy,3)/1000) + ' kWh')
        self.gPVIDC.AutoSize()

        self.gPVITemps.ClearGrid()
        self.gPVITemps.DeleteRows(numRows=self.gPVITemps.GetNumberRows())
        self.gPVITemps.AppendRows(data['PVI_TEMPERATURE_COUNT'].data)
        for d in data:
            if d.name == 'PVI_TEMPERATURE':
                index = d['PVI_INDEX'].data
                self.gPVITemps.SetCellValue(index,0,str(round(d['PVI_VALUE'],3)) + ' °C')
                self.gPVITemps.SetRowLabelValue(index, u"Temperatur #" + str(index))

        self.gPVITemps.AutoSize()



    def fill_bat(self):
        logger.debug('Rufe BAT-Daten ab')
        self._data_bat = []
        for index in [0,1]:
            try:
                requests = self.gui.getBatDcbData(bat_index=index)
                if len(requests) > 0:
                    f = self.gui.get_data(requests, True)
                    self._data_bat.append(f)
                    logger.info('Erfolgreich BAT #' + str(index) + ' abgerufen')
            except:
                logger.exception('Fehler beim Abruf von BAT #' + str(index))

        self.cbBATIndex.Clear()

        for bat in self._data_bat:
            self.cbBATIndex.Append('BAT #' + str(bat['BAT_INDEX'].data))

        logger.debug('BAT-Daten abgerufen')


    def fill_bat_index(self, index):
        logger.debug('Fülle BAT-Datenfelder')

        self.gDCB_row_voltages = None
        self.gDCB_row_temp = None
        self.gDCB_last_row = 28

        f = self._data_bat[index]

        #if index == 1:
        #    f['BAT_DCB_COUNT'].data = 1

        dcbcount = int(f['BAT_DCB_COUNT'])
        self.gDCB.DeleteCols(pos=0, numCols=self.gDCB.GetNumberCols())
        self.txtUsableCapacity.SetValue(str(round(f['BAT_USABLE_CAPACITY'], 5)) + ' Ah')
        self.txtUsableRemainingCapacity.SetValue(str(round(f['BAT_USABLE_REMAINING_CAPACITY'], 5)) + ' Ah')
        self.txtASOC.SetValue(str(round(f['BAT_ASOC'], 1)) + '%')
        self.txtFCC.SetValue(str(round(f['BAT_FCC'], 3)))
        self.txtRC.SetValue(str(round(f['BAT_RC'], 1)) + '%')
        self.txtRSOC.SetValue(str(round(f['BAT_INFO']['BAT_RSOC'], 1)) + '%')
        self.txtRSOCREAL.SetValue(str(round(f['BAT_RSOC_REAL'], 1)) + '%')
        self.txtModuleVoltage.SetValue(str(round(f['BAT_INFO']['BAT_MODULE_VOLTAGE'], 3)) + ' V')
        self.txtCurrent.SetValue(str(round(f['BAT_INFO']['BAT_CURRENT'], 5)) + ' A')
        self.txtBatStatusCode.SetValue(repr(f['BAT_INFO']['BAT_STATUS_CODE']))
        self.txtErrorCode.SetValue(repr(f['BAT_INFO']['BAT_ERROR_CODE']))
        self.txtDcbCount.SetValue(repr(f['BAT_DCB_COUNT']))
        self.gDCB.AppendCols(dcbcount)
        for i in range(0, dcbcount):
            self.gDCB.SetColLabelValue(i, 'DCB #' + str(i))
        self.txtMaxBatVoltage.SetValue(repr(f['BAT_MAX_BAT_VOLTAGE']) + ' V')
        self.txtMaxChargeCurrent.SetValue(repr(f['BAT_MAX_CHARGE_CURRENT']) + ' A')
        self.txtEodVoltage.SetValue(repr(f['BAT_EOD_VOLTAGE']) + ' V')
        self.txtMaxDischargeCurrent.SetValue(repr(f['BAT_MAX_DISCHARGE_CURRENT']) + ' A')
        self.txtChargeCycles.SetValue(repr(f['BAT_CHARGE_CYCLES']))
        self.txtTerminalVoltage.SetValue(repr(f['BAT_TERMINAL_VOLTAGE']) + ' V')
        self.txtMaxDcbCellTemperature.SetValue(str(round(f['BAT_MAX_DCB_CELL_TEMPERATURE'], 2)) + ' °C')
        self.txtMinDcbCellTemperature.SetValue(str(round(f['BAT_MIN_DCB_CELL_TEMPERATURE'], 2)) + ' °C')

        self.chBATDeviceConnected.SetValue(f['BAT_DEVICE_STATE']['BAT_DEVICE_CONNECTED'].data)
        self.chBATDeviceWorking.SetValue(f['BAT_DEVICE_STATE']['BAT_DEVICE_WORKING'].data)
        self.chBATDeviceInService.SetValue(f['BAT_DEVICE_STATE']['BAT_DEVICE_IN_SERVICE'].data)

        self.txtBATMaxDCBCount.SetValue(repr(f['BAT_SPECIFICATION']['BAT_SPECIFIED_MAX_DCB_COUNT']))
        self.txtBATCapacity.SetValue(repr(f['BAT_SPECIFICATION']['BAT_SPECIFIED_CAPACITY']) + ' Wh')
        self.txtBATMaxChargePower.SetValue(repr(f['BAT_SPECIFICATION']['BAT_SPECIFIED_CHARGE_POWER']) + ' W')
        self.txtBATMaxDischargePower.SetValue(repr(f['BAT_SPECIFICATION']['BAT_SPECIFIED_DSCHARGE_POWER']) + ' W')

        self.txtBATMeasuredResistance.SetValue(repr(f['BAT_INTERNALS']['BAT_MEASURED_RESISTANCE']))
        self.txtBATRunMeasuredResistance.SetValue(repr(f['BAT_INTERNALS']['BAT_RUN_MEASURED_RESISTANCE']))

        if f['BAT_TRAINING_MODE'].data == 0:
            s = 'Nicht im Training'
        elif f['BAT_TRAINING_MODE'].data == 1:
            s = 'Trainingsmodus Entladen'
        elif f['BAT_TRAINING_MODE'].data == 2:
            s = 'Trainingsmodus Laden'
        else:
            s = ' - '
        self.txtBatTrainingMode.SetValue(s)
        self.txtBatDeviceName.SetValue(repr(f['BAT_DEVICE_NAME']))

        def set_voltages(d):
            if d.type == RSCPType.Error:
                logger.warning('BAT_DCB_ALL_CELL_VOLTAGES konnte nicht abgerufen werden')
            else:
                index = int(d['BAT_DCB_INDEX'])
                if index < dcbcount:
                    if index == 0 and not self.gDCB_row_voltages:
                        rows = self.gDCB.GetNumberRows()
                        if rows < self.gDCB_last_row + len(d['BAT_DATA']):
                            self.gDCB.AppendRows(len(d['BAT_DATA']))
                        self.gDCB_row_voltages = self.gDCB_last_row
                        self.gDCB_last_row += len(d['BAT_DATA'])

                    i = 1
                    for volt in d['BAT_DATA']:
                        self.gDCB.SetRowLabelValue(self.gDCB_row_voltages + i, u"Spannung #" + str(i))
                        self.gDCB.SetCellValue(self.gDCB_row_voltages + i, index, str(round(volt, 4)) + ' V')
                        i += 1

        def set_temperatures(d):
            if d.type == RSCPType.Error:
                logger.warning('BAT_DCB_ALL_CELL_TEMPERATURES konnte nicht abgerufen werden')
            else:
                index = int(d['BAT_DCB_INDEX'])
                if index < dcbcount:
                    if index == 0 and not self.gDCB_row_temp:
                        rows = self.gDCB.GetNumberRows()
                        if rows < self.gDCB_last_row + len(d['BAT_DATA']):
                            self.gDCB.AppendRows(len(d['BAT_DATA']))
                        self.gDCB_row_temp = self.gDCB_last_row
                        self.gDCB_last_row += len(d['BAT_DATA'])

                    i = 1
                    for temp in d['BAT_DATA']:
                        self.gDCB.SetRowLabelValue(self.gDCB_row_temp + i, u"Temperatur #" + str(i))
                        self.gDCB.SetCellValue(self.gDCB_row_temp + i, index, str(round(temp, 4)) + ' °C')
                        i += 1

        def set_info(d):
            if d.type == RSCPType.Error:
                logger.warning('BAT_DCB_INFO konnte nicht abgerufen werden')
            else:
                index = int(d['BAT_DCB_INDEX'])
                if index < dcbcount:
                    dd = datetime.datetime.fromtimestamp(int(d['BAT_DCB_LAST_MESSAGE_TIMESTAMP']) / 1000)
                    self.gDCB.SetCellValue(0, index, str(dd))
                    self.gDCB.SetCellValue(1, index, repr(d['BAT_DCB_MAX_CHARGE_VOLTAGE']) + ' V')
                    self.gDCB.SetCellValue(2, index, repr(d['BAT_DCB_MAX_CHARGE_CURRENT']) + ' A')
                    self.gDCB.SetCellValue(3, index, repr(d['BAT_DCB_END_OF_DISCHARGE']) + ' V')
                    self.gDCB.SetCellValue(4, index, repr(d['BAT_DCB_MAX_DISCHARGE_CURRENT']) + ' A')
                    self.gDCB.SetCellValue(5, index, str(round(d['BAT_DCB_FULL_CHARGE_CAPACITY'], 5)) + ' Ah')
                    self.gDCB.SetCellValue(6, index, str(round(d['BAT_DCB_REMAINING_CAPACITY'], 5)) + ' Ah')
                    self.gDCB.SetCellValue(7, index, repr(d['BAT_DCB_SOC']) + '%')
                    self.gDCB.SetCellValue(8, index, repr(d['BAT_DCB_SOH']) + '%')
                    self.gDCB.SetCellValue(9, index, repr(d['BAT_DCB_CYCLE_COUNT']))
                    self.gDCB.SetCellValue(10, index, str(round(d['BAT_DCB_CURRENT'], 5)) + ' A')
                    self.gDCB.SetCellValue(11, index, str(round(d['BAT_DCB_VOLTAGE'], 2)) + ' V')
                    self.gDCB.SetCellValue(12, index, str(round(d['BAT_DCB_CURRENT_AVG_30S'], 5)) + ' A')
                    self.gDCB.SetCellValue(13, index, str(round(d['BAT_DCB_VOLTAGE_AVG_30S'], 2)) + ' V')
                    self.gDCB.SetCellValue(14, index, str(round(d['BAT_DCB_DESIGN_CAPACITY'], 5)) + ' Ah')
                    self.gDCB.SetCellValue(15, index, repr(d['BAT_DCB_DESIGN_VOLTAGE']) + ' V')
                    self.gDCB.SetCellValue(16, index, repr(d['BAT_DCB_CHARGE_LOW_TEMPERATURE']) + ' °C')
                    self.gDCB.SetCellValue(17, index, repr(d['BAT_DCB_CHARGE_HIGH_TEMPERATURE']) + ' °C')
                    self.gDCB.SetCellValue(18, index, repr(d['BAT_DCB_MANUFACTURE_DATE']))
                    self.gDCB.SetCellValue(19, index, repr(d['BAT_DCB_SERIALNO']))
                    self.gDCB.SetCellValue(20, index, repr(d['BAT_DCB_FW_VERSION']))
                    self.gDCB.SetCellValue(21, index, repr(d['BAT_DCB_PCB_VERSION']))
                    self.gDCB.SetCellValue(22, index, repr(d['BAT_DCB_DATA_TABLE_VERSION']))
                    self.gDCB.SetCellValue(23, index, repr(d['BAT_DCB_PROTOCOL_VERSION']))
                    self.gDCB.SetCellValue(24, index, repr(d['BAT_DCB_NR_SERIES_CELL']))
                    self.gDCB.SetCellValue(25, index, repr(d['BAT_DCB_NR_PARALLEL_CELL']))
                    self.gDCB.SetCellValue(26, index, repr(d['BAT_DCB_SERIALCODE']))
                    self.gDCB.SetCellValue(27, index, repr(d['BAT_DCB_NR_SENSOR']))
                    self.gDCB.SetCellValue(28, index, repr(d['BAT_DCB_STATUS']))

        if dcbcount > 1:
            for d in f['BAT_DCB_ALL_CELL_TEMPERATURES']:
                set_temperatures(d)
            for d in f['BAT_DCB_ALL_CELL_VOLTAGES']:
                set_voltages(d)
            for d in f['BAT_DCB_INFO']:
                set_info(d)
        else:
            set_temperatures(f['BAT_DCB_ALL_CELL_TEMPERATURES'])
            set_voltages(f['BAT_DCB_ALL_CELL_VOLTAGES'])
            set_info(f['BAT_DCB_INFO'])

        self.gDCB.AutoSizeColumns()
        logger.debug('BAT-Datenfelder füllen abgeschlossen')

    def bSaveClick(self, event):
        self.saveConfig()

        #rscp = self.gui.get_data(self.gui.getUserLevel(), True)
        #ems = self.gui.get_data(self.gui.getEMSData() + self.gui.getUserLevel(), True)

    def bSYSRebootOnClick( self, event ):
        res = wx.MessageBox('Soll das gesamte E3/DC - System wirklich neu gestartet werden?', 'Systemneustart', wx.YES_NO | wx.ICON_WARNING)
        if res == wx.YES:
            try:
                r = RSCPTag.SYS_REQ_SYSTEM_REBOOT
                print(r)
                res = self.gui.get_data([r], True)
                print(res)
                wx.MessageBox('System wird neu gestartet')
            except:
                traceback.print_exc()
                wx.MessageBox('Übertragung fehlgeschlagen')

    def bSYSApplicationRestartOnClick( self, event ):
        res = wx.MessageBox('Soll die Anwendung im E3/DC wirklich neu gestartet werden?', 'Applikations - Neustart', wx.YES_NO | wx.ICON_WARNING)
        if res == wx.YES:
            try:
                r = RSCPTag.SYS_REQ_RESTART_APPLICATION
                print(r)
                res = self.gui.get_data([r], True)
                print(res)
                wx.MessageBox('Anwendung wird neu gestartet')
            except:
                traceback.print_exc()
                wx.MessageBox('Übertragung fehlgeschlagen')

    def bEMSEPTestOnClick( self, event ):
        res = wx.MessageBox('Beim Notstromtest wird kurz der Hausstrom gekappt.\nNach etwa 10-15 Sekunden sollte dann '
                            'der Notstrom einspringen und\nStrom wieder zur Verfügung stehen.\nEs wird dann auf Notstrom'
                            ' umgestellt. Nach dem Test wird der\nNotstrom wieder beendet, der Hausstrom wird wieder '
                            'getrennt für ca. 2 Sekunden.\n\nSoll der Notstrom-Test wirklich durchgeführt werden?',
                            'Notstrom-Test', wx.YES_NO | wx.ICON_WARNING)
        if res == wx.YES:
            try:
                r = RSCPDTO(tag = RSCPTag.REQ_START_EMERGENCYPOWER_TEST, rscp_type=RSCPType.Bool, data = True)
                print(r)
                res = self.gui.get_data([r], True)
                print(res)
                wx.MessageBox('Notstromtest wird durchgeführt')
            except:
                traceback.print_exc()
                wx.MessageBox('Übertragung fehlgeschlagen')

    def bEMSManualChargeStartOnClick( self, event ):
        try:
            val = int(self.txtEMSManualChargeValue.GetValue())
        except:
            val = 0
        if val > 0:
            res = wx.MessageBox('Die manuelle Ladung ist auf die Ausführung einmal am Tag begrenzt, steht nicht genügend PV-Leistung zur Verfügung, wird Netzstrom bezogen! Fortfahren?', 'Manuelle Ladung', wx.YES_NO | wx.ICON_QUESTION)
            if res == wx.YES:
                try:
                    r = RSCPDTO(tag = RSCPTag.EMS_REQ_START_MANUAL_CHARGE, rscp_type = RSCPType.Uint32, data = val)
                    print(r)
                    res = self.gui.get_data([r], True)
                    print(res)
                    if res.name == 'EMS_START_MANUAL_CHARGE' and res.data:
                        wx.MessageBox('Manuelle Ladung gestartet')
                    else:
                        wx.MessageBox('Fehler beim Start der manuellen Ladung, heute bereits durchgeführt?')
                except:
                    traceback.print_exc()
                    wx.MessageBox('Übertragung fehlgeschlagen')
        else:
            wx.MessageBox('Die Ladung von (' + self.txtEMSManualChargeValue.GetValue() + ') Wh ist nicht zulässig, bitte anderen Ganzzahl-Wert wählen', 'Manuelle Ladung', wx.ICON_WARNING)

    def bTestClick(self, event):
        self.disableButtons()

        try:
            if not self.gui:
                raise Exception('Keine Verbindungsart möglich')

            logger.debug('Teste Verbindung')
            result = self.gui.get_data(self.gui.getInfo(), True)

            sn = repr(result['INFO_SERIAL_NUMBER'])
            ip = repr(result['INFO_IP_ADDRESS'])
            rel = repr(result['INFO_SW_RELEASE'])

            if isinstance(self.gui, E3DCWebGui) and self._connectiontype == 'web':
                msg = wx.MessageBox('Verbindung per Web mit System ' + sn + ' / ' + rel + ' hergestellt', 'Info',
                                    wx.OK | wx.ICON_INFORMATION)
            elif isinstance(self.gui, E3DCWebGui) and self.txtRSCPPassword.GetValue() == '':
                msg = wx.MessageBox('Verbindung mit System ' + sn + ' / ' + rel + ' konnte nur über WebSockets hergestellt werden, da kein RSCP-Passwort vergeben wurde.\nDiese Verbindung ist langsamer und auf das Internet angewiesen.\n', 'Info',
                                    wx.OK | wx.ICON_WARNING)
            elif isinstance(self.gui, E3DCWebGui) and self.txtRSCPPassword.GetValue() != '':
                msg = wx.MessageBox(
                    'Verbindung mit System ' + sn + ' / ' + rel + ' konnte nur über WebSockets hergestellt werden, da das angegebene RSCP-Passwort falsch ist oder das E3DC nicht direkt erreichbar ist.\nDiese Verbindung ist langsamer und auf das Internet angewiesen.',
                    'Info',
                    wx.OK | wx.ICON_WARNING)
            else:
                msg = wx.MessageBox('Verbindung direkt mit System  ' + sn + ' / ' + rel + ' hergestellt', 'Info',
                          wx.OK | wx.ICON_INFORMATION)

            logger.debug('Verbindungstest erfolgreich. Verbindungsart: ' + self.connectiontype)
        except:
            logger.exception('Verbindungstest nicht erfolgreich')
            msg = wx.MessageBox('Verbindung konnte nicht aufgebaut werden', 'Error',
                                wx.OK | wx.ICON_ERROR)

        self.enableButtons()

    def bUpdateCheckClick(self, event):
        try:
            result = self.gui.get_data(self.gui.getCheckForUpdates(), True)
            status = result.data
        except:
            traceback.print_exc()
            status = 0

        if status == 1:
            wx.MessageBox('Updatecheck wird ausgeführt, wenn eine neue Version zur Verfügung steht wird diese installiert.')
        else:
            wx.MessageBox('Updatecheck konnte nicht ausgeführt werden')

    def clear_values(self):
        self._data_bat = []
        self._data_dcdc = []
        self._data_ems = None
        self._data_info = None
        self._data_pvi = {}
        self._data_pm = {}
        self._data_wb = []

    def disableButtons(self):
        logger.debug('Deaktiviere Buttons')
        self.enableDisableButtons(False)

    def enableButtons(self):
        logger.debug('Aktiviere Buttons')
        self.enableDisableButtons(True)
        self.set_wb_enabled()

    def enableDisableButtons(self, value= True):
        self.bUpdate.Enable(value)
        self.bEMSEPTest.Enable(value)
        self.bEMSManualChargeStart.Enable(value)
        self.bSave.Enable(value)
        self.bTest.Enable(value)
        self.bConfigGetIPAddress.Enable(value)
        self.bConfigGetSerialNo.Enable(value)
        self.bConfigSetRSCPPassword.Enable(value)
        self.bEMSUploadChanges.Enable(value)
        self.bINFOSave.Enable(value)
        self.bSaveRSCPData.Enable(value)
        self.bSYSApplicationRestart.Enable(value)
        self.bSYSReboot.Enable(value)
        self.btnUpdatecheck.Enable(value)
        self.bUpload.Enable(value)
        self.bWBSave.Enable(value)
        self.bWBStopLoading.Enable(value)
        self.bUploadStart.Enable(value)
        self.bUploadSetData.Enable(value)

    def sWBLadestromOnScroll( self, event ):
        self.stWBLadestrom.SetLabel(str(self.sWBLadestrom.GetValue()) + ' A')

    def bUpdateClick(self, event = None):
        page = self.pMainregister.GetCurrentPage()
        name = page.GetName()
        if name == 'CONFIG':
            self._updatethread = threading.Thread(target=self.updateData, args=())
            self._updatethread.start()
        else:
            self.pMainChanged()

    def updateData(self):
        if not self._updateRunning:
            self._updateRunning = True
            self.disableButtons()
            self.gaUpdate.SetValue(0)
            try:
                self.gaUpdate.SetValue(5)
                if self.gui:
                    self.gaUpdate.SetValue(10)
                    logger.info('Aktualisiere Daten')
                    self.clear_values()

                    try:
                        self.fill_info()
                    except:
                        logger.exception('Fehler beim Abruf der INFO-Daten')

                    self.gaUpdate.SetValue(20)
                    try:
                        selected = self.cbBATIndex.GetSelection()
                        if selected in [wx.NOT_FOUND, '', None, False]:
                            selected = 0

                        self.fill_bat()

                        if selected != wx.NOT_FOUND:
                            if self.cbBATIndex.GetCount() > selected:
                                self.cbBATIndex.SetSelection(selected)
                                self.fill_bat_index(selected)
                    except:
                        logger.exception('Fehler beim Abruf der BAT-Daten')

                    self.gaUpdate.SetValue(35)
                    try:
                        self.fill_dcdc()
                    except:
                        logger.exception('Fehler beim Abruf der DCDC-Daten')

                    self.gaUpdate.SetValue(45)
                    try:
                        selected = self.chPVIIndex.GetSelection()
                        if selected in [wx.NOT_FOUND, '', None, False]:
                            selected = 0

                        self.fill_pvi()

                        if selected != wx.NOT_FOUND:
                            if self.chPVIIndex.GetCount() > selected:
                                self.chPVIIndex.SetSelection(selected)
                                self.fill_pvi_index(selected)
                    except:
                        logger.exception('Fehler beim Abruf der PVI-Daten')

                    self.gaUpdate.SetValue(55)
                    try:
                        self.fill_ems()
                        self.fill_mbs()
                    except:
                        logger.exception('Fehler beim Abruf der EMS-Daten')

                    self.gaUpdate.SetValue(70)
                    try:
                        self.fill_pm()
                    except:
                        logger.exception('Fehler beim Abruf der PM-Daten')

                    self.gaUpdate.SetValue(85)
                    try:
                        selected = self.cbWallbox.GetSelection()
                        if selected in [wx.NOT_FOUND, '', None, False]:
                            selected = 0

                        self.fill_wb()

                        if selected != wx.NOT_FOUND:
                            if self.cbWallbox.GetCount() > selected:
                                self.cbWallbox.SetSelection(selected)
                                self.fill_wb_index(selected)
                    except:
                        logger.exception('Fehler beim Abruf der WB-Daten')

                    try:
                        self.fill_mbs()
                    except:
                        logger.exception('Fehler beim Abruf der Modbus-Daten')

                    self.gaUpdate.SetValue(90)
                else:
                    logger.warning('Konfiguration unvollständig, Verbindung nicht möglich')

            except:
                logger.exception('Fehler beim Aktualisieren der Daten')
            self.gaUpdate.SetValue(100)
            self.enableButtons()
            self._updateRunning = False

    def bMBSSaveOnClick( self, event ):
        r = []
        test = self.chMBSEnabled.GetValue()
        if test != self._data_mbs['MBS_MODBUS_ENABLED'].data:
            r.append(RSCPDTO(tag = RSCPTag.MBS_REQ_SET_MODBUS_ENABLED, rscp_type=RSCPType.Bool, data=test))

        if len(r) == 0:
            res = wx.MessageBox('Es wurden keine Änderungen gemacht, aktuelle Einstellungen trotzdem übertragen?', 'Modbus-Einstellungen speichern', wx.YES_NO)
            if res == wx.YES:
                test = self.chMBSEnabled.GetValue()
                r.append(RSCPDTO(tag=RSCPTag.MBS_REQ_SET_MODBUS_ENABLED, rscp_type=RSCPType.Bool, data=test))

        if len(r) > 0:
            res = wx.MessageBox('Eine Änderung der Modbus-Einstellungen unterbricht kurz die Kommunikation, laufende RSCP-Verbindungen werden direkt beendet. Fortfahren?', 'Modbus-Einstellungen speichern', wx.YES_NO | wx.ICON_WARNING)
            if res == wx.YES:
                try:
                    res = self.gui.get_data(r, True)
                    wx.MessageBox('Übertragung abgeschlossen')
                except:
                    traceback.print_exc()
                    wx.MessageBox('Übertragung fehlgeschlagen')

                self.pMainChanged()

        event.Skip()


    def bSaveRSCPDataOnClick( self, event ):
        with wx.FileDialog(self, "als JSON speichern", wildcard="JSON files (*.json)|*.json",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as fileDialog:

            if fileDialog.ShowModal() == wx.ID_CANCEL:
                return  # the user changed their mind

            # save the current contents in the file
            pathname = fileDialog.GetPath()
            try:
                data = self.sammle_data()

                with open(pathname, 'w') as file:
                    json.dump(data, file)

            except IOError:
                logger.exception('Daten konnten nicht in Datei gespeichert werden ' + pathname)
                wx.LogError("Cannot save current data in file '%s'." % pathname)


        event.Skip()

    def sendToServer(self, event):
        ret = wx.MessageBox('Achtung, es werden alle angezeigten Daten an externe Stelle übermittelt.\nSeriennummern werden in anonymisierter Form übermittelt.\nZugangsdaten werden nicht übermittelt.\nDie Datenübertragung erfolgt verschlüsselt.\nWirklich fortfahren?',
                            caption = 'Ausgelesene Daten übermitteln',
                            style=wx.YES_NO)
        if ret == wx.YES:
            logger.debug('Sende Daten an Server')
            status = 'NO CODE'
            try:
                data = self.sammle_data()

                r = requests.post(url = self.txtDBServer.GetValue(), json = data)
                r.raise_for_status()
                res = r.json()
                if 'error' in res:
                    raise Exception('Fehler bei Datenübermittlung' + res['error'])

                if 'success' in res:
                    logger.debug('Datenübertragung erfolgreich Speicherpfad: ' + res['success'])
                    MessageBox(self, 'Übermittlung Erfolgreich', 'Daten wurden erfolgreich übermittelt!\nSpeicherpfad:\n\n' + res['success'])


            except:
                logger.exception('Fehler bei der Datenübertragung an Server ' + self.txtDBServer.GetValue())
                wx.MessageBox('Es gab einen Fehler bei der Übermittlung. (HTTP-Status: ' + str(r.status_code) + ')')

    def sammle_data(self, anon = True):
        logger.debug('Sammle Daten')
        self.updateData()

        anonymize = ['DCDC_SERIAL_NUMBER', 'INFO_MAC_ADDRESS', 'BAT_DCB_SERIALNO', 'BAT_DCB_SERIALCODE', 'INFO_SERIAL_NUMBER',
                     'INFO_A35_SERIAL_NUMBER', 'PVI_SERIAL_NUMBER', 'INFO_PRODUCTION_DATE']
        remove = ['INFO_IP_ADDRESS']
        data = {}
        if self._data_bat:
            data['BAT_DATA'] = []
            for d in self._data_bat:
                data['BAT_DATA'].append(d.asDict())

        if self._data_dcdc:
            data['DCDC_DATA'] = []
            for d in self._data_dcdc:
                data['DCDC_DATA'].append(d.asDict())

        if self._data_ems:
            data['EMS_DATA'] = self._data_ems.asDict()

        if self._data_info:
            data['INFO_DATA'] = self._data_info.asDict()

        if self._data_pvi:
            data['PVI_DATA'] = {}
            for k in self._data_pvi:
                d = self._data_pvi[k]
                data['PVI_DATA'][k] = d.asDict()

        if self._data_pm:
            data['PM_DATA'] = {}
            for k in self._data_pm:
                d = self._data_pm[k]
                data['PM_DATA'][k] = d.asDict()

        if self._data_wb:
            data['WB_DATA'] = []
            for d in self._data_wb:
                data['WB_DATA'].append(d.asDict())
        if anon:
            logger.debug('Anonymisiere Daten')
            data = self.anonymize_data(data, anonymize, remove)
            logger.debug('Daten wurden anonymisiert')
        logger.debug('Datensammlung beendet')
        return data

    def anonymize_data(self, data, anonymize, remove):
        if isinstance(data, dict):
            toremove = []
            for i in data.keys():
                if isinstance(data[i], dict) or isinstance(data[i], list):
                    data[i] = self.anonymize_data(data[i], anonymize, remove)
                elif i in anonymize:
                    if isinstance(data[i], int) or isinstance(data[i], float):
                        data[i] = str(data[i])
                    if len(data[i]) >= 6:
                        tmp = 'X' * (len(data[i])-6)
                        data[i] = data[i][:6] + tmp
                    else:
                        data[i] = 'X' * len(data[i])
                elif i in remove:
                    toremove.append(i)

            for r in toremove:
                del data[r]
        elif isinstance(data, list):
            nl = []
            for i in data:
                nl += [self.anonymize_data(i, anonymize, remove)]
            data = nl
        return data


    def mainOnClose(self, event):
        if isinstance(self._gui, E3DCWebGui):
            self._gui.e3dc.ws.close()

        event.Skip()


    def getSerialnoFromWeb(self, username, password):
        logger.debug('Ermittle Seriennummer über Webzugriff')
        userlevel = None

        try:
            r = requests.post('https://s10.e3dc.com/s10/phpcmd/cmd.php', data={'DO': 'LOGIN',
                                                                               'USERNAME': username,
                                                                               'PASSWD': hashlib.md5(password.encode()).hexdigest(),
                                                                               'DENV': 'E3DC'})
            r.raise_for_status()
            r_json = r.json()
            if r_json['ERRNO'] != 0:
                raise Exception('Abfrage Fehlerhaft #1, Fehlernummer ' + str(r_json['ERRNO']))
            userlevel = int(r_json['CONTENT']['USERLEVEL'])
            cookies = r.cookies
            if userlevel in (1, 128):
                r = requests.post('https://s10.e3dc.com/s10/phpcmd/cmd.php', data={'DO': 'GETCONTENT',
                                                                                   'MODID': 'IDOVERVIEWCOMMONTABLE',
                                                                                   'ARG0': 'undefined',
                                                                                   'TOS': -7200,
                                                                                   'DENV': 'E3DC'}, cookies=cookies)
                r.raise_for_status()
                r_json = r.json()

                if r_json['ERRNO'] != 0:
                    raise Exception('Abfrage fehlerhaft #2, Fehlernummer ' + str(r_json['ERRNO']))

                content = r_json['CONTENT']
                html = None
                for lst in content:
                    if 'HTML' in lst:
                        html = lst['HTML']
                        break

                if not html:
                    raise Exception('Abfrage Fehlerhaft #3, Daten nicht gefunden')

                regex = r"s10list = '(\[\{.*\}\])';"

                try:
                    match = re.search(regex, html, re.MULTILINE).group(1)
                    obj = json.loads(match)
                    return obj
                except:
                    raise Exception('Abfrage Fehlerhaft #4, Regex nicht erfolgreich')

        except:
            logger.exception('Fehler beim Abruf der Seriennummer, Zugangsdaten fehlerhaft?')

        return []

    def bConfigGetSerialNoOnClick( self, event ):
        username = self.txtUsername.GetValue()
        password = self.txtPassword.GetValue()
        if not username and not password:
            wx.MessageBox('Zur Ermittlung der Seriennummer sind mindestens Benutzername und Passwort erforderlich!', 'Ermittlung Seriennummer', wx.ICON_WARNING)
        else:
            serial = None

            if self.gui:
                try:
                    requests = []
                    requests.append(RSCPTag.INFO_REQ_SERIAL_NUMBER)
                    result = self.gui.get_data(requests, True)
                    if result.name == 'INFO_SERIAL_NUMBER':
                        serial = result.data
                        self.txtConfigSeriennummer.SetValue(serial)
                        wx.MessageBox('Seriennummer konnte ermittelt werden (RSCP): ' + serial, 'Ermittlung Seriennummer')
                except:
                    logger.exception('Fehler beim Abruf der Seriennummer')

            if username and password and not serial:
                ret = self.getSerialnoFromWeb(username, password)
                if len(ret) == 1:
                    serial = self.getSNFromNumbers(ret[0]['serialno'])
                    self.txtConfigSeriennummer.SetValue(serial)
                    wx.MessageBox('Seriennummer konnte ermittelt werden (WEB): ' + serial, 'Ermittlung Seriennummer')
                elif len(ret) > 1:
                    sns = '\n'
                    for sn in ret:
                        sns += '\n' + self.getSNFromNumbers(sn['serialno'])
                    wx.MessageBox('Es wurde mehr als eine Seriennummer ermittelt (WEB):' + sns, 'Ermittlung Seriennummer')
                    serial = len(ret)

            if not serial:
                wx.MessageBox('Es konnte keine Seriennummer ermittelt werden (WEB). Zugangsdaten falsch?',
                              'Ermittlung Seriennummer', wx.ICON_ERROR)

        event.Skip()

    def getSNFromNumbers(self, sn):
        if sn[0:2] == '70':
            return 'P10-' + sn
        else:
            return 'S10-' + sn

    def bConfigGetIPAddressOnClick( self, event ):
        try:
            ip = repr(self.gui.get_data(self.gui.getInfo(), True)['INFO_IP_ADDRESS'])
            if ip:
                self.txtIP.SetValue(ip)
                wx.MessageBox('IP-Adresse konnte ermittelt werden: ' + ip, 'Ermittlung IP-Adresse')
            else:
                wx.MessageBox('IP-Adresse konnte nicht ermittelt werden, kein Inhalt', 'Ermittlung IP-Adresse', wx.ICON_ERROR)

        except:
            wx.MessageBox('Bei der Ermittlung der IP-Adresse ist ein Fehler aufgetreten #2', 'Ermittlung IP-Adresse', wx.ICON_ERROR)

    def bConfigSetRSCPPasswordOnClick( self, event ):
        ret = wx.MessageBox('Soll das angegebene RSCP-Kennwort mit dem bisherigen überschrieben werden?', 'RSCP-Passwort ändern', wx.YES_NO | wx.ICON_WARNING)
        if ret == wx.YES:
            try:
                password = self.txtRSCPPassword.GetValue()
                requests = [RSCPDTO(tag = RSCPTag.RSCP_REQ_SET_ENCRYPTION_PASSPHRASE, rscp_type = RSCPType.CString, data = password)]
                res = self.gui.get_data(requests, True)
                if res.data:
                    wx.MessageBox('RSCP-Passwort wurde erfolgreich geändert', 'RSCP-Passwort ändern', wx.ICON_INFORMATION)
                else:
                    raise Exception('RSCP-Passwort wurde nicht akzeptiert!')
            except:
                wx.MessageBox('Fehler beim Ändern des RSCP-Passworts', 'RSCP-Passwort ändern', wx.ICON_ERROR)


        event.Skip()

    def check_e3dcwebgui(self):
        while True:
            try:
                if self.connectiontype == 'web':
                    try:
                        if self.gui.e3dc.connected:
                            self._connected = True
                            self.txtConfigAktiveVerbindung.SetValue('web - active')
                        else:
                            self._connected = False
                            self.txtConfigAktiveVerbindung.SetValue('web - inactive')
                    except:
                        self._connected = None
                        self.txtConfigAktiveVerbindung.SetValue('unknown')
                elif self.connectiontype == 'direkt':
                    self._connected = True
                    self.txtConfigAktiveVerbindung.SetValue('direct')
                else:
                    self._connected = False
                    self.txtConfigAktiveVerbindung.SetValue('no con')
            except RuntimeError:
                logger.debug('Beende check_e3dcwebgui')
                os._exit(1)
            except:
                self._connected = None
                logger.exception('check_e3dcwebgui')

            time.sleep(2)

    def cbBATIndexOnCombobox( self, event ):
        if not self._updateRunning:
            self._updateRunning = True
            selected = self.cbBATIndex.GetSelection()
            if selected != wx.NOT_FOUND:
                self.fill_bat_index(selected)
            self._updateRunning = False

        event.Skip()

    def chPVIIndexOnCombobox( self, event ):
        if not self._updateRunning:
            self._updateRunning = True
            selected = self.chPVIIndex.GetSelection()
            if selected != wx.NOT_FOUND:
                self.fill_pvi_index(selected)
            self._updateRunning = False

        event.Skip()

    def bINFOSaveOnClick( self, event ):
        r = []
        test = self.cbTimezone.GetValue()
        if test != self._data_info['INFO_TIME_ZONE'].data:
            r.append(RSCPDTO(tag = RSCPTag.INFO_REQ_SET_TIME_ZONE, rscp_type=RSCPType.CString, data=test))

        test = self.txtIPAdress.GetValue()
        if test != self._data_info['INFO_IP_ADDRESS'].data:
            r.append(RSCPDTO(tag = RSCPTag.INFO_REQ_SET_IP_ADDRESS, rscp_type=RSCPType.CString, data=test))

        test = self.txtSubnetmask.GetValue()
        if test != self._data_info['INFO_SUBNET_MASK'].data:
            r.append(RSCPDTO(tag = RSCPTag.INFO_REQ_SET_SUBNET_MASK, rscp_type=RSCPType.CString, data=test))

        test = self.txtGateway.GetValue()
        if test != self._data_info['INFO_GATEWAY'].data:
            r.append(RSCPDTO(tag = RSCPTag.INFO_REQ_SET_GATEWAY, rscp_type=RSCPType.CString, data=test))

        test = self.txtDNSServer.GetValue()
        if test != self._data_info['INFO_DNS'].data:
            r.append(RSCPDTO(tag = RSCPTag.INFO_REQ_SET_DNS, rscp_type=RSCPType.CString, data=test))

        test = self.chDHCP.GetValue()
        if test != self._data_info['INFO_DHCP_STATUS'].data:
            r.append(RSCPDTO(tag = RSCPTag.INFO_REQ_SET_DHCP_STATUS, rscp_type=RSCPType.Bool, data=test))

        if len(r) == 0:
            res = wx.MessageBox('Es wurden keine Änderungen gemacht, aktuelle Einstellungen trotzdem übertragen?', 'Info speichern', wx.YES_NO)
            if res == wx.YES:
                test = self.cbTimezone.GetValue()
                r.append(RSCPDTO(tag=RSCPTag.INFO_REQ_SET_TIME_ZONE, rscp_type=RSCPType.CString, data=test))

                test = self.txtIPAdress.GetValue()
                r.append(RSCPDTO(tag=RSCPTag.INFO_REQ_SET_IP_ADDRESS, rscp_type=RSCPType.CString, data=test))

                test = self.txtSubnetmask.GetValue()
                r.append(RSCPDTO(tag=RSCPTag.INFO_REQ_SET_SUBNET_MASK, rscp_type=RSCPType.CString, data=test))

                test = self.txtGateway.GetValue()
                r.append(RSCPDTO(tag=RSCPTag.INFO_REQ_SET_GATEWAY, rscp_type=RSCPType.CString, data=test))

                test = self.txtDNSServer.GetValue()
                r.append(RSCPDTO(tag=RSCPTag.INFO_REQ_SET_DNS, rscp_type=RSCPType.CString, data=test))

                test = self.chDHCP.GetValue()
                r.append(RSCPDTO(tag=RSCPTag.INFO_REQ_SET_DHCP_STATUS, rscp_type=RSCPType.Bool, data=test))

        if len(r) > 0:
            res = wx.MessageBox('Wichtig: Falsche Einstellungen können dazu führen, dass das E3DC nicht mehr oder nur noch per Websockets zu erreichen ist. Die Einstellungen müssen dann über das Display direkt geändert werden. Wirklich durchführen?', 'Hinweis', wx.ICON_WARNING + wx.YES_NO)
            if res == wx.YES:
                try:
                    res = self.gui.get_data(r, True)
                    wx.MessageBox('Übertragung abgeschlossen')
                except:
                    traceback.print_exc()
                    wx.MessageBox('Übertragung fehlgeschlagen')

                self.pMainChanged()


logger.debug('Module geladen, initialisiere App')
app = wx.App()
logger.debug('App initialisiert, lade Fenster')
g = Frame(None, args)
logger.debug('Fenster geladen')
if not args.hide:
    logger.debug('zeichne Fenster')
    g.Show()
    logger.debug('Fenster gezeichnet, warte auf Events')
app.MainLoop()

logger.debug('Programm beendet')