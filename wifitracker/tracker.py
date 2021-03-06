from collections import OrderedDict
import csv
import datetime
import json
import logging
import os.path
from threading import Thread
from itertools import islice
try:
    from queue import Queue  # try python3
except ImportError:
    from Queue import Queue

# http requests for humans:
import requests

log = logging.getLogger(__name__)
logging.getLogger('requests').setLevel(logging.WARNING)


class ProbeRequest(object):

    def __init__(self, source_mac, capture_dts,
                 target_ssid=None, signal_strength=None):
        self.capture_dts = capture_dts
        self.source_mac = source_mac
        self.target_ssid = target_ssid
        self.signal_strength = signal_strength

    def __str__(self):
        return "SENDER='{}', SSID='{}', RSSi={}".format(self.source_mac,
                                                        self.target_ssid,
                                                        self.signal_strength)

    def __jdict__(self):
        dts = datetime.datetime.strftime(self.capture_dts,
                                         '%Y-%m-%d %H:%M:%S.%f')
        return OrderedDict([('source_mac', self.source_mac),
                            ('capture_dts', dts),
                            ('target_ssid', self.target_ssid),
                            ('signal_strength', self.signal_strength)])


class Device(object):

    def __init__(self, device_mac, last_seen_dts=None, known_ssids=None,
                 vendor_company=None, vendor_country=None, alias=None):
        self.device_mac = device_mac
        self.known_ssids = known_ssids if known_ssids else []
        self.vendor_company = vendor_company
        self.vendor_country = vendor_country
        self.last_seen_dts = last_seen_dts
        self.alias = alias

    def set_vendor(self, session=None):
        """Set the vendor of this device. The vendor can be looked up by the
        devices mac address.

        Keyword arguments:
        session -- HTTPS session with connections which should be reused for the
                   requests neccesary for the lookup.
        """
        try:
            vendor = _lookup_vendor(self.device_mac, session)
            self.vendor_company = vendor['company']
            self.vendor_country = vendor['country']
        except Exception:
            log.warn("Unable to lookup vendor for: {}".format(self.device_mac))
            self.vendor_company = None
            self.vendor_country = None

    def set_alias(self, alias):
        if not self.alias:
            self.alias = alias
            log.debug("Set alias of device ({}) to: {}".format(self.device_mac,
                                                               self.alias))

    def add_ssid(self, ssid):
        """Add a new SSID to the device.

        ssid -- string object
        """
        if ssid and ssid not in self.known_ssids:
            self.known_ssids.append(ssid)
            log.debug('SSID added to device:{}'.format(ssid))

    def __str__(self):
        return "MAC='{}', vendor='{} [{}]'".format(self.device_mac,
                                                   self.vendor_company,
                                                   self.vendor_country)

    def __jdict__(self):
        dts = datetime.datetime.strftime(self.last_seen_dts,
                                         '%Y-%m-%d %H:%M:%S.%f')
        return OrderedDict([('device_mac', self.device_mac),
                            ('alias', self.alias),
                            ('known_ssids', self.known_ssids),
                            ('last_seen_dts', dts),
                            ('vendor_company', self.vendor_company),
                            ('vendor_country', self.vendor_country)])


class Station(object):

    def __init__(self, ssid, associated_devices=None):
        self.ssid = ssid
        self.associated_devices = []
        if associated_devices:
            self.associated_devices = associated_devices

    def add_device(self, device_mac):
        """Add a known assoiciated device to the station."""
        if device_mac and device_mac not in self.associated_devices:
            self.associated_devices.append(device_mac)
            log.debug("Device added to station:{}@'{}'".format(device_mac,
                                                               self.ssid))

    def __str__(self):
        return "SSID='{}'".format(self.ssid)

    def __jdict__(self):
        return OrderedDict([('ssid', self.ssid),
                            ('associated_devices', self.associated_devices)])


class Tracker(object):

    def __init__(self, storage_dir):
        self.storage_dir = storage_dir
        self.request_filename = os.path.join(self.storage_dir, 'requests')
        self.alias_filename = os.path.join(self.storage_dir, 'aliases.csv')

    def add_request(self, request):
        """Add the captured request to the tracker. The tracker might store this
        request in a file or database backend.
        """
        # TODO: store in mongodb/send over REST
        self._write_request(request)

    def _write_request(self, request):
        dump = json_compact(request)
        with open(self.request_filename, 'a') as file:
            file.write('\n' + dump)

    def get_devices(self, load_dts=None, aliases=None):
        """Load a version of all devices valid at the given timestamp."""
        devices = {}
        aliases = {} if not aliases else aliases
        for request_chunk in self._read_requests_chunk(load_dts):
            for request in request_chunk:
                id = request.source_mac
                capture_dts = request.capture_dts
                ssid = request.target_ssid
                if id not in devices:
                    devices[id] = Device(id, last_seen_dts=capture_dts)
                    log.debug("new device: {}".format(devices[id]))
                    if id in aliases:
                        devices[id].set_alias(aliases[id])
                if ssid:
                    devices[id].add_ssid(ssid)
                if devices[id].last_seen_dts < capture_dts:
                    devices[id].last_seen_dts = capture_dts
        return devices

    def get_device(self, device_mac, load_dts=None, alias=None):
        device = Device(device_mac, alias=alias)
        for request_chunk in self._read_requests_chunk(load_dts):
            device_requests = [r for r in request_chunk
                               if r.source_mac == device_mac]
            for request in device_requests:
                if request.target_ssid:
                    device.add_ssid(request.target_ssid)
            try:
                device.last_seen_dts = device_requests[-1].capture_dts
            except IndexError:
                # ignore if list is empty
                pass
        return device

    def get_stations(self, load_dts=None):
        """Load a version of all stations valid at the given timestamp."""
        stations = {}
        for request_chunk in self._read_requests_chunk(load_dts):
            for request in request_chunk:
                ssid = request.target_ssid
                device_mac = request.source_mac
                if ssid:
                    if ssid not in stations:
                        stations[ssid] = Station(ssid)
                        log.debug("new station: {}".format(stations[ssid]))
                    stations[ssid].add_device(device_mac)
        return stations

    def get_station(self, ssid, load_dts=None):
        station = Station(ssid)
        for request_chunk in self._read_requests_chunk(load_dts):
            station_requests = [r for r in request_chunk
                                if r.target_ssid == ssid]
            for request in station_requests:
                device_mac = request.source_mac
                station.add_device(device_mac)
        return station

    def get_aliases(self):
        aliases = {}
        with open(self.alias_filename, 'rb') as csvfile:
            reader = csv.reader(csvfile, delimiter=';', quotechar='"')
            for row in reader:
                aliases[row[0]] = row[1]
        return aliases

    def set_device_alias(self, device_mac, alias, force=False):
        aliases = self.get_aliases()
        if not force and device_mac in aliases:
            raise ValueError("Device alias already set.")
        else:
            aliases[device_mac] = alias
            with open(self.alias_filename, 'wb') as csvfile:
                writer = csv.writer(csvfile, delimiter=';', quotechar='"')
                for d in aliases:
                    writer.writerow([d, aliases[d]])

    def _read_requests_chunk(self, load_dts=None, chunk_size=10000):
        if not load_dts:
            load_dts = datetime.datetime.now()
        chunk_no = 0
        with open(self.request_filename) as file:
            while True:
                chunk = list(islice(file, chunk_size))
                if not chunk:
                    break
                chunk_no += 1
                lines = [line for line in chunk if len(line) > 1]
                try:
                    all = _load_requests('[' + ','.join(lines) + ']')
                except:
                    # try to decode line by line
                    all = []
                    i = 0
                    for line in lines:
                        i += 1
                        try:
                            all += _load_requests('[' + line + ']')
                        except Exception:
                            # ignore erroneous lines
                            line_no = chunk_size * (chunk_no - 1) + i
                            log.error("Unable to decode line at {}:{}".format(
                                self.request_filename, line_no))
                if all[0].capture_dts > load_dts:
                    # abort since we assume the requests are sorted
                    break
                yield [r for r in all if r.capture_dts < load_dts]


def _load_requests(dump):
    decoded = json.loads(dump)
    requests = []
    for d in decoded:
        try:
            capture_dts = _strptime(d['capture_dts'])
        except:
            capture_dts = None
        target_ssid = d['target_ssid']
        if target_ssid:
            target_ssid = repr(target_ssid)[2:-1]
        request = ProbeRequest(d['source_mac'], capture_dts,
                               target_ssid=target_ssid,
                               signal_strength=d['signal_strength'])
        requests.append(request)
    return requests


def _lookup_vendor(device_mac, session=None):
    session = session if session else requests.Session()
    lookup_url = 'https://www.macvendorlookup.com/api/v2/' + device_mac
    vendor_response = session.get(lookup_url, timeout=10).json()[0]
    return vendor_response


def set_vendors(devices, workers=100):
    """Lookup the vendors for each device in a dict of devices.
    The lookup requests are executed in parallel for better performance when
    handling many devices.

    Keyword arguments:
    workers -- number of lookups which should be done in parallel
    """

    class VendorLookupThread(Thread):
        """Helper class for concurrent vendor lookup."""

        def __init__(self, queue, session):
            super(VendorLookupThread, self).__init__()
            self.queue = queue
            self.session = session

        def run(self):
            while True:
                device = self.queue.get()
                device.set_vendor(self.session)
                self.queue.task_done()

    # the session is used to reuse https connections:
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=workers,
                                            pool_maxsize=workers,
                                            pool_block=True)
    session.mount('https://', adapter)

    queue = Queue(workers)

    for i in xrange(0, workers):
        thread = VendorLookupThread(queue, session)
        thread.setDaemon(True)
        thread.start()
    for id in devices:
        queue.put(devices[id])
    queue.join()
    session.close()


def _strptime(s):
    """Parse datetime strings of the format 'YYYY-MM-DD hh:mm:ss.ssssss'.
    This is less flexible but more performant than datetime.datetime.strptime.
    """
    return datetime.datetime(year=int(s[:4]),
                             month=int(s[5:7]),
                             day=int(s[8:10]),
                             hour=int(s[11:13]),
                             minute=int(s[14:16]),
                             second=int(s[17:19]),
                             microsecond=int(s[20:26]))


def json_pretty(obj):
    """Generate pretty json string with indentions and spaces."""
    return json.dumps(obj.__jdict__(), indent=4, separators=(',', ': '))


def json_compact(obj):
    """Generate compact json string without whitespaces."""
    return json.dumps(obj.__jdict__(), separators=(',', ':'))
