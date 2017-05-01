# encoding=utf8
import datetime
from distutils.version import StrictVersion
import hashlib
import os.path
import random
from seesaw.config import realize, NumberConfigValue
from seesaw.externalprocess import ExternalProcess
from seesaw.item import ItemInterpolation, ItemValue
from seesaw.task import SimpleTask, LimitConcurrent
from seesaw.tracker import GetItemFromTracker, PrepareStatsForTracker, \
    UploadWithTracker, SendDoneToTracker
import shutil
import socket
import subprocess
import sys
import time
import string
import re

try:
    import requests
except:
    raise Exception("Please install requests with 'pip install requests'.")
try:
    import warc
except:
    raise Exception("Please install warc with 'pip install warc'.")

import seesaw
from seesaw.externalprocess import WgetDownload
from seesaw.pipeline import Pipeline
from seesaw.project import Project
from seesaw.util import find_executable


# check the seesaw version
if StrictVersion(seesaw.__version__) < StrictVersion("0.8.5"):
    raise Exception("This pipeline needs seesaw version 0.8.5 or higher.")


###########################################################################
# Find a useful Wget+Lua executable.
#
# WGET_LUA will be set to the first path that
# 1. does not crash with --version, and
# 2. prints the required version string
WGET_LUA = find_executable(
    "Wget+Lua",
    ["GNU Wget 1.14.lua.20130523-9a5c", "GNU Wget 1.14.lua.20160530-955376b"],
    [
        "./wget-lua",
        "./wget-lua-warrior",
        "./wget-lua-local",
        "../wget-lua",
        "../../wget-lua",
        "/home/warrior/wget-lua",
        "/usr/bin/wget-lua"
    ]
)

if not WGET_LUA:
    raise Exception("No usable Wget+Lua found.")


###########################################################################
# The version number of this pipeline definition.
#
# Update this each time you make a non-cosmetic change.
# It will be added to the WARC files and reported to the tracker.
VERSION = "20170501.01"
USER_AGENT = 'ArchiveTeam'
TRACKER_ID = 'flickr'
TRACKER_HOST = 'tracker.archiveteam.org'


###########################################################################
# This section defines project-specific tasks.
#
# Simple tasks (tasks that do not need any concurrency) are based on the
# SimpleTask class and have a process(item) method that is called for
# each item.
class CheckIP(SimpleTask):
    def __init__(self):
        SimpleTask.__init__(self, "CheckIP")
        self._counter = 0

    def process(self, item):
        # NEW for 2014! Check if we are behind firewall/proxy

        if self._counter <= 0:
            item.log_output('Checking IP address.')
            ip_set = set()

            ip_set.add(socket.gethostbyname('twitter.com'))
            ip_set.add(socket.gethostbyname('facebook.com'))
            ip_set.add(socket.gethostbyname('youtube.com'))
            ip_set.add(socket.gethostbyname('microsoft.com'))
            ip_set.add(socket.gethostbyname('icanhas.cheezburger.com'))
            ip_set.add(socket.gethostbyname('archiveteam.org'))

            if len(ip_set) != 6:
                item.log_output('Got IP addresses: {0}'.format(ip_set))
                item.log_output(
                    'Are you behind a firewall/proxy? That is a big no-no!')
                raise Exception(
                    'Are you behind a firewall/proxy? That is a big no-no!')

        # Check only occasionally
        if self._counter <= 0:
            self._counter = 10
        else:
            self._counter -= 1


class PrepareDirectories(SimpleTask):
    def __init__(self, warc_prefix):
        SimpleTask.__init__(self, "PrepareDirectories")
        self.warc_prefix = warc_prefix

    def process(self, item):
        item_name = item["item_name"]
        escaped_item_name = item_name.replace(':', '_').replace('/', '_').replace('~', '_')
        item_hash = hashlib.sha1(item_name.encode('utf-8')).hexdigest()
        dirname = "/".join((item["data_dir"], item_hash))

        if os.path.isdir(dirname):
            shutil.rmtree(dirname)

        os.makedirs(dirname)

        item["item_dir"] = dirname
        item["warc_file_base"] = "%s-%s-%s" % (self.warc_prefix, item_hash,
            time.strftime("%Y%m%d-%H%M%S"))

        open("%(item_dir)s/%(warc_file_base)s.warc.gz" % item, "w").close()


class Deduplicate(SimpleTask):
    def __init__(self):
        SimpleTask.__init__(self, "Deduplicate")

    def process(self, item):
        hashes = {}
        input_filename = "%(item_dir)s/%(warc_file_base)s.warc.gz" % item
        output_filename = "%(item_dir)s/%(warc_file_base)s-deduplicated.warc.gz" % item

        warc_input = warc.WARCFile(input_filename)
        warc_input_size = os.path.getsize(input_filename)
        warc_output = warc.WARCFile(output_filename, 'w')
        dedup_log = []

        info_record = warc_input.read_record()
        info_record.header['WARC-Filename'] = "%(warc_file_base)s-deduplicated.warc.gz" % item
        del info_record.header['WARC-Block-Digest']
        warc_output.write_record(warc.WARCRecord(
            payload=info_record.payload.read(),
            header=info_record.header))

        while warc_input_size > warc_input.tell():
            for record in warc_input:
                if record.type == 'response':
                    hash_ = record.header.get('WARC-Payload-Digest').split(':', 1)[1]
                    if hash_ in hashes:
                        headers = []
                        payload_ = record.payload.read()
                        for line in payload_.splitlines():
                            if line in ['\r\n', '\n', '']:
                                break
                            headers.append(line.strip())
                        payload = '\r\n'.join(headers) + '\r\n'*2
                        if not ('Content-Length: 0' in payload or \
                              'content-length: 0' in payload):
                            record.header['Content-Length'] = str(len(payload))
                            record.header['WARC-Refers-To'] = hashes[hash_][0]
                            record.header['WARC-Refers-To-Date'] = hashes[hash_][1]
                            record.header['WARC-Refers-To-Target-URI'] = \
                                hashes[hash_][2]
                            record.header['WARC-Type'] = 'revisit'
                            record.header['WARC-Truncated'] = 'length'
                            record.header['WARC-Profile'] = 'http://netpreserve' \
                                '.org/warc/1.0/revisit/identical-payload-digest'
                            del record.header['WARC-Block-Digest']
                            dedup_log.append('WARC-Record-ID:{dID}; ' \
                                'WARC-Target-URI:{dURL}; WARC-Date:{dDate} ' \
                                'duplicate of WARC-Record-ID:{oID}; ' \
                                'WARC-Target-URI:{oURL}; WARC-Date:{oDate}\r\n' \
                                .format(dID=record.header['WARC-Record-ID'],
                                dURL=record.header['WARC-Target-URI'],
                                dDate=record.header['WARC-Date'],
                                oID=hashes[hash_][0], oURL=hashes[hash_][2],
                                oDate=hashes[hash_][1]))
                            record = warc.WARCRecord(header=record.header,
                                payload=payload, defaults=False)
                        else:
                            record = warc.WARCRecord(header=record.header,
                                payload=payload_, defaults=False)
                    else:
                        hashes[hash_] = (record.header.get('WARC-Record-ID'),
                            record.header.get('WARC-Date'),
                            record.header.get('WARC-Target-URI'))
                        record = warc.WARCRecord(
                            header=record.header,
                            payload=record.payload.read(), defaults=False)
                else:
                    record = warc.WARCRecord(header=record.header,
                        payload=record.payload.read(), defaults=False)
                warc_output.write_record(record)
        with open("%(item_dir)s/deduplicate.log" % item, 'w') as f:
            f.write('\r\n'.join(dedup_log))


class MoveFiles(SimpleTask):
    def __init__(self):
        SimpleTask.__init__(self, "MoveFiles")

    def process(self, item):
        # NEW for 2014! Check if wget was compiled with zlib support
        if os.path.exists("%(item_dir)s/%(warc_file_base)s.warc" % item):
            raise Exception('Please compile wget with zlib support!')

        os.rename("%(item_dir)s/%(warc_file_base)s-deduplicated.warc.gz" % item,
            "%(data_dir)s/%(warc_file_base)s-deduplicated.warc.gz" % item)

        shutil.rmtree("%(item_dir)s" % item)


def get_hash(filename):
    with open(filename, 'rb') as in_file:
        return hashlib.sha1(in_file.read()).hexdigest()


CWD = os.getcwd()
PIPELINE_SHA1 = get_hash(os.path.join(CWD, 'pipeline.py'))
LUA_SHA1 = get_hash(os.path.join(CWD, 'flickr.lua'))


def stats_id_function(item):
    # NEW for 2014! Some accountability hashes and stats.
    d = {
        'pipeline_hash': PIPELINE_SHA1,
        'lua_hash': LUA_SHA1,
        'python_version': sys.version,
    }

    return d


class WgetArgs(object):
    def realize(self, item):
        wget_args = [
            WGET_LUA,
            "-U", USER_AGENT,
            "-nv",
            "--no-cookies",
            "--lua-script", "flickr.lua",
            "-o", ItemInterpolation("%(item_dir)s/wget.log"),
            "--no-check-certificate",
            "--output-document", ItemInterpolation("%(item_dir)s/wget.tmp"),
            "--truncate-output",
            "-e", "robots=off",
            "--rotate-dns",
            "--recursive", "--level=inf",
            "--no-parent",
            "--page-requisites",
            "--timeout", "30",
            "--tries", "inf",
            "--domains", "flickr.com",
            "--span-hosts",
            "--waitretry", "30",
            "--warc-file", ItemInterpolation("%(item_dir)s/%(warc_file_base)s"),
            "--warc-header", "operator: Archive Team",
            "--warc-header", "flickr-dld-script-version: " + VERSION,
            "--warc-header", ItemInterpolation("flickr-item: %(item_name)s"),
        ]
        
        item_name = item['item_name']
        assert ':' in item_name
        item_type, item_value = item_name.split(':', 1)
        
        item['item_type'] = item_type
        item['item_value'] = item_value
        
        assert item_type in ('images')

        if item_type == 'images':
            images = item_value.split(',')
            for image in images:
                photo_splitted = image.split('/')
                photo_user_id = photo_splitted[0]

                tries = 0
                while tries < 15:
                    photo_user_response = requests.get('https://www.flickr.com/photos/{0}/'.format(photo_user_id))

                    if photo_user_response.status_code == 418:
                        wait_time = random.randint(1, 15)
                        print('Flickr turned into a teapot. Retrying for coffee in {0} seconds.'.format(wait_time))
                        sys.stdout.flush()
                        tries += 1
                        time.sleep(wait_time)
                        continue
                    elif (len(photo_user_response.text) == 0 \
                          or photo_user_response.status_code != 200) \
                          and not photo_user_response.status_code == 404:
                        print('Photo {0}.'.format(photo_user_id))
                        print('Received status code {0}.'.format(photo_user_response.status_code))
                        print('Received {0} bytes.'.format(len(photo_user_response.text)))
                        raise Exception('Something went wrong... ABORTING')

                    break
                else:
                    raise Exception('Failed to brew coffee with flickr... :(')

                if photo_user_response.status_code == 404:
                    continue

                photo_user = re.search(r'<meta\s+property="og:url"\s+content="https://www\.flickr\.com/photos/([^/]+)/"\s+data-dynamic="true">',
                  photo_user_response.text).group(1)
                photo_id = photo_splitted[1]

                print('Found photo {photo_id} from user {photo_user} with user ID {photo_user_id}.'.format(**locals()))
                sys.stdout.flush()

                wget_args.extend(['--warc-header', 'flickr-photo-item: {image}'.format(**locals())])
                wget_args.extend(['--warc-header', 'flickr-photo: {photo_id}'.format(**locals())])
                wget_args.extend(['--warc-header', 'flickr-photo-user-id: {photo_user_id}'.format(**locals())])
                wget_args.extend(['--warc-header', 'flickr-photo-user: {photo_user}'.format(**locals())])
                wget_args.extend(['--warc-header', 'flickr-photo-{photo_id}-user: {photo_user}'.format(**locals())])
                wget_args.append('https://www.flickr.com/photos/{photo_user}/{photo_id}/'.format(**locals()))
                #wget_args.append('https://www.flickr.com/photos/{photo_user_id}/{photo_id}/'.format(**locals()))
                wget_args.append('https://www.flickr.com/photos/{photo_user}/{photo_id}/in/photostream/'.format(**locals()))
                #wget_args.append('https://www.flickr.com/photos/{photo_user_id}/{photo_id}/in/photostream/'.format(**locals()))
                wget_args.append('https://www.flickr.com/photos/{photo_user}/{photo_id}/in/photostream/lightbox/'.format(**locals()))
                #wget_args.append('https://www.flickr.com/photos/{photo_user_id}/{photo_id}/in/photostream/lightbox/'.format(**locals()))
                wget_args.append('https://www.flickr.com/photos/{photo_user}/{photo_id}/sizes/'.format(**locals()))
                #wget_args.append('https://www.flickr.com/photos/{photo_user_id}/{photo_id}/sizes/'.format(**locals()))
                wget_args.append('https://www.flickr.com/video_download.gne?id={photo_id}'.format(**locals()))
                item['item_value'] += ',' + photo_user + '/' + photo_id
        else:
            raise Exception('Unknown item')
        
        if 'bind_address' in globals():
            wget_args.extend(['--bind-address', globals()['bind_address']])
            print('')
            print('*** Wget will bind address at {0} ***'.format(
                globals()['bind_address']))
            print('')
            
        return realize(wget_args, item)

###########################################################################
# Initialize the project.
#
# This will be shown in the warrior management panel. The logo should not
# be too big. The deadline is optional.
project = Project(
    title="flickr",
    project_html="""
        <img class="project-logo" alt="Project logo" src="http://archiveteam.org/images/thumb/0/03/Flick_logo_black.png/320px-Flick_logo_black.png" height="50px" title=""/>
        <h2>flickr.com <span class="links"><a href="http://flickr.com/">Website</a> &middot; <a href="http://tracker.archiveteam.org/flickr/">Leaderboard</a></span></h2>
        <p>Archiving CC photos from flickr.</p>
    """
)

pipeline = Pipeline(
    CheckIP(),
    GetItemFromTracker("http://%s/%s" % (TRACKER_HOST, TRACKER_ID), downloader,
        VERSION),
    PrepareDirectories(warc_prefix="flickr"),
    WgetDownload(
        WgetArgs(),
        max_tries=2,
        accept_on_exit_code=[0, 4, 8],
        env={
            "item_dir": ItemValue("item_dir"),
            "item_value": ItemValue("item_value"),
            "item_type": ItemValue("item_type"),
            "random_number": str(random.randint(0, 500)),
        }
    ),
    Deduplicate(),
    PrepareStatsForTracker(
        defaults={"downloader": downloader, "version": VERSION},
        file_groups={
            "data": [
                ItemInterpolation("%(item_dir)s/%(warc_file_base)s-deduplicated.warc.gz")
            ]
        },
        id_function=stats_id_function,
    ),
    MoveFiles(),
    LimitConcurrent(NumberConfigValue(min=1, max=4, default="1",
        name="shared:rsync_threads", title="Rsync threads",
        description="The maximum number of concurrent uploads."),
        UploadWithTracker(
            "http://%s/%s" % (TRACKER_HOST, TRACKER_ID),
            downloader=downloader,
            version=VERSION,
            files=[
                ItemInterpolation("%(data_dir)s/%(warc_file_base)s-deduplicated.warc.gz")
            ],
            rsync_target_source_path=ItemInterpolation("%(data_dir)s/"),
            rsync_extra_args=[
                "--recursive",
                "--partial",
                "--partial-dir", ".rsync-tmp",
            ]
            ),
    ),
    SendDoneToTracker(
        tracker_url="http://%s/%s" % (TRACKER_HOST, TRACKER_ID),
        stats=ItemValue("stats")
    )
)
