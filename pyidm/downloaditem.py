"""
    pyIDM

    multi-connections internet download manager, based on "pyCuRL/curl", "youtube_dl", and "PySimpleGUI"

    :copyright: (c) 2019-2020 by Mahmoud Elshahat.
    :license: GNU LGPLv3, see LICENSE for more details.
"""

# Download Item Class

import os
import mimetypes
import time
from collections import deque
from queue import Queue
from threading import Thread, Lock
from urllib.parse import urljoin
from .utils import validate_file_name, get_headers, translate_server_code, size_splitter, get_seg_size, log, \
    delete_file, delete_folder, save_json, load_json, size_format
from . import config


class Segment:
    def __init__(self, name=None, num=None, range=None, size=None, url=None, tempfile=None, seg_type='', merge=True):
        self.name = name  # full path file name
        # self.basename = os.path.basename(self.name)
        self.num = num
        self.size = size
        self.range = range
        self.downloaded = False
        self.completed = False  # done downloading and merging into tempfile
        self.tempfile = tempfile
        self.headers = {}
        self.url = url
        self.seg_type = seg_type
        self.merge = merge
        self.key = None

    @property
    def basename(self):
        if self.name:
            return os.path.basename(self.name)
        else:
            return 'undefined'

    def get_size(self):
        self.headers = get_headers(self.url)
        try:
            self.size = int(self.headers.get('content-length', 0))
            print('Segment num:', self.num, 'getting size:', self.size)
        except:
            pass
        return self.size

    def __repr__(self):
        return repr(self.__dict__)


class DownloadItem:

    # animation ['►►   ', '  ►►'] › ► ⤮ ⇴ ↹ ↯  ↮  ₡ ['⯈', '▼', '⯇', '▲']
    # ['⏵⏵', '  ⏵⏵'] ['›', '››', '›››', '››››', '›››››'] ['❯', '❯❯', '❯❯❯', '❯❯❯❯'] ['|', '||', '|||', '||||', '|||||']
    animation_icons = {config.Status.downloading: ['❯' * n for n in range(1, 5)],
                       config.Status.pending: ['⏳'],
                       config.Status.completed: ['✔'], config.Status.cancelled: ['-x-'],
                       config.Status.processing: ['↯', '↯↯', '↯↯↯'], config.Status.error: ['err']}

    def __init__(self, id_=0, url='', name='', folder=''):
        self.id = id_
        self._name = name
        self.ext = ''

        self.folder = os.path.abspath(folder)

        self.url = url
        self.eff_url = ''

        self.size = 0
        self._total_size = 0
        self.resumable = False

        # type and subtypes
        self.type = ''  # general, video, audio
        self.subtype_list = []  # it might contains one or more eg "hls, dash, fragmented, normal"

        self._segment_size = config.segment_size

        self.live_connections = 0
        self._downloaded = 0
        self._lock = None  # Lock() to access downloaded property from different threads
        self._status = config.Status.cancelled
        self._remaining_parts = 0

        # connection status
        self.status_code = 0
        self.status_code_description = ''

        # animation
        self.animation_index = 0
        self.animation_timer = 0

        # audio
        self.audio_stream = None
        self.audio_url = None
        self.audio_size = 0
        self.is_audio = False
        self.audio_quality = None

        # postprocessing callback is a string represent any function name need to be called after done downloading
        # this function must be available or imported in brain.py namespace
        self.callback = ''

        # schedule download
        self.sched = None  # should be time in (hours, minutes) tuple for scheduling download

        # speed
        self._speed = 0
        self.prev_downloaded_value = 0
        self.speed_buffer = deque()  # store some speed readings for calculating average speed afterwards
        self.speed_timer = 0
        self.speed_refresh_rate = 1  # calculate speed every n seconds

        # segments
        self._segments = []

        # fragmented video parameters will be updated from video subclass object / update_param()
        self.fragment_base_url = None
        self.fragments = None

        # fragmented audio parameters will be updated from video subclass object / update_param()
        self.audio_fragment_base_url = None
        self.audio_fragments = None

        # protocol
        self.protocol = ''

        # format id, youtube-dl specific
        self.format_id = None
        self.audio_format_id = None

        # quality for video and audio
        self.abr = None
        self.tbr = None  # for video equal Bandwidth/1000
        self.resolution = None  # for videos only example for 720p: 1280x720

        # hls m3u8 manifest url
        self.manifest_url = ''

        # thumbnails
        self.thumbnail_url = None
        self.thumbnail = None  # base64 string

        # playlist info
        self.playlist_url = ''
        self.playlist_title = ''

        # selected stream name for video objects
        self.selected_quality = None

        # subtitles
        # template: {language1:[sub1, sub2, ...], language2: [sub1, ...]}, where sub = {'url': 'xxx', 'ext': 'xxx'}
        self.subtitles = {}
        self.automatic_captions = {}
        self.selected_subtitles = {}  # chosen subtitles that will be downloaded

        # accept html contents
        self.accept_html = False  # if server sent html contents instead of bytes

        # errors
        self.errors = 0  # an indicator for server, network, or other errors while downloading

        # subprocess references
        self.subprocess = None

        # test
        self.seg_names = []

        # properties names that will be saved on disk
        self.saved_properties = ['id', '_name', 'folder', 'url', 'eff_url', 'playlist_url', 'playlist_title', 'size',
                                 'resumable', 'selected_quality', '_segment_size', '_downloaded', '_status',
                                 '_remaining_parts', 'audio_url', 'audio_size', 'type', 'subtype_list', 'fragments',
                                 'fragment_base_url', 'audio_fragments', 'audio_fragment_base_url',
                                 '_total_size', 'protocol', 'manifest_url', 'selected_subtitles',
                                 'abr', 'tbr', 'format_id', 'audio_format_id', 'resolution', 'audio_quality']

        # property to indicate that there is a time consuming operation is running on download item now
        self.busy = False

    # def __getattr__(self, attrib):  # commented out as it makes problem with copy.copy module
    #     """this method will be called if no attribute found"""
    #
    #     # will return empty string instead of raising error
    #     return ''

    def select_subs(self, subs_names=None):
        """
        search subtitles names and build a dict of name:url for all selected subs
        :param subs_names: list of subs names
        :return: None
        """
        if not isinstance(subs_names, list):
            return

        subs = {}
        # search for subs
        for k in subs_names:
            v = self.subtitles.get(k) or self.automatic_captions.get(k)
            if v:
                subs[k] = v

        self.selected_subtitles = subs

        # print('self.selected_subtitles:', self.selected_subtitles)

    @property
    def remaining_parts(self):
        return self._remaining_parts

    @remaining_parts.setter
    def remaining_parts(self, value):
        self._remaining_parts = value

        # should recalculate total size again with every completed segment, most of the time segment size won't be
        # available until actually downloaded this segment, "check worker.report_completed()"
        self.total_size = self.calculate_total_size()

    @property
    def segments(self):
        if not self._segments:
            # don't handle hls videos
            if 'hls' in self.subtype_list:
                return self._segments

            # handle fragmented video
            if self.fragments:
                # print(self.fragments)
                # example 'fragments': [{'path': 'range/0-640'}, {'path': 'range/2197-63702', 'duration': 9.985},]
                self._segments = [Segment(name=os.path.join(self.temp_folder, str(i)), num=i, range=None, size=0,
                                          url=urljoin(self.fragment_base_url, x.get('path', '')), tempfile=self.temp_file)
                                  for i, x in enumerate(self.fragments)]

            else:
                if self.resumable and self.size:
                    # get list of ranges i.e. ['0-100', 101-2000' ... ]
                    range_list = size_splitter(self.size, self.segment_size)
                else:
                    range_list = [None]  # add None in a list to make one segment with range=None

                self._segments = [
                    Segment(name=os.path.join(self.temp_folder, str(i)), num=i, range=x, size=get_seg_size(x),
                            url=self.eff_url, tempfile=self.temp_file)
                    for i, x in enumerate(range_list)]

            # get an audio stream to be merged with dash video
            if 'dash' in self.subtype_list:
                # handle fragmented audio
                if self.audio_fragments:
                    # example 'fragments': [{'path': 'range/0-640'}, {'path': 'range/2197-63702', 'duration': 9.985},]
                    audio_segments = [
                        Segment(name=os.path.join(self.temp_folder, str(i) + '_audio'), num=i, range=None, size=0,
                                url=urljoin(self.audio_fragment_base_url, x.get('path', '')), tempfile=self.audio_file)
                        for i, x in enumerate(self.audio_fragments)]

                else:
                    range_list = size_splitter(self.audio_size, self.segment_size)

                    audio_segments = [
                        Segment(name=os.path.join(self.temp_folder, str(i) + '_audio'), num=i, range=x,
                                size=get_seg_size(x), url=self.audio_url, tempfile=self.audio_file)
                        for i, x in enumerate(range_list)]

                # append to main list
                self._segments += audio_segments

        # for debugging purpose only
        if self._segments:
            seg_names = [seg.basename for seg in self._segments]
            if seg_names != self.seg_names:
                log(f'Segments-{self.name}, ({len(seg_names)}):', seg_names, log_level=3)
                self.seg_names = seg_names
        return self._segments

    @segments.setter
    def segments(self, value):
        self._segments = value

    @property
    def total_size(self):
        # recalculate total size only if there is size change in segment size
        if not self._total_size:
            self._total_size = self.calculate_total_size()

        return self._total_size

    @total_size.setter
    def total_size(self, value):
        self._total_size = value

    def calculate_total_size(self):
        total_size = 0

        # this is heavy and should be used carefully, calculate size by getting every segment's size
        if self.segments:
            sizes = [seg.size for seg in self.segments if seg.size]
            total_size = sum(sizes)
            # if there is some items not yet downloaded and have zero size will make estimated calculations
            if sizes and [seg for seg in self.segments if seg.downloaded is False and not seg.size]:
                avg_seg_size = sum(sizes) // len(sizes)
                total_size = avg_seg_size * len(self.segments)  # estimated

        return total_size

    @property
    def speed(self):
        """return an average of some speed values will give a stable speed reading"""
        if self.status != config.Status.downloading:  # or not self.speed_buffer:
            self._speed = 0
        else:
            if not self.prev_downloaded_value:
                self.prev_downloaded_value = self.downloaded

            time_passed = time.time() - self.speed_timer
            if time_passed >= self.speed_refresh_rate:
                self.speed_timer= time.time()
                delta = self.downloaded - self.prev_downloaded_value
                self.prev_downloaded_value = self.downloaded
                _speed = delta / time_passed

                # to get a stable speed reading will use an average of multiple speed readings
                self.speed_buffer.append(_speed)
                avg_speed = sum(self.speed_buffer) / len(self.speed_buffer)
                if len(self.speed_buffer) > 10:
                    self.speed_buffer.popleft()

                if avg_speed:
                    self._speed = avg_speed if avg_speed > 0 else 0

        return self._speed

    @property
    def lock(self):
        # Lock() to access downloaded property from different threads
        if not self._lock:
            self._lock = Lock()
        return self._lock

    @property
    def downloaded(self):
        return self._downloaded

    @downloaded.setter
    def downloaded(self, value):
        """this property might be set from threads, expecting int (number of bytes)"""
        if not isinstance(value, int):
            return

        with self.lock:
            self._downloaded = value

    @property
    def progress(self):
        p = 0

        if self.status == config.Status.completed:
            p = 100

        elif self.total_size == 0 and self.segments:
            # to handle fragmented files
            finished = len([seg for seg in self.segments if seg.completed])
            p = round(finished * 100 / len(self.segments), 1)
        elif self.total_size:
            p = round(self.downloaded * 100 / self.total_size, 1)

        # make progress 99% if not completed
        if p >= 100:
            if not self.status == config.Status.completed:
                p = 99
            else:
                p = 100

        return p

    @property
    def time_left(self):
        if self.status == config.Status.downloading and self.total_size and self.total_size >= self.downloaded:
            return (self.total_size - self.downloaded) / self.speed if self.speed else -1
        else:
            return '---'

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        self._status = value

        # kill subprocess if currently active
        if self.subprocess and value in (config.Status.cancelled, config.Status.error):
            self.kill_subprocess()

    @property
    def num(self):
        return self.id + 1 if isinstance(self.id, int) else self.id

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, new_value):
        # validate new name
        self._name = validate_file_name(new_value)

        # Reset segments since change in file name will affect segments info
        self.segments.clear()

    @property
    def target_file(self):
        """return file name including path"""
        return os.path.join(self.folder, self.name)

    @property
    def temp_file(self):
        """return temp file name including path"""
        name = f'_temp_{self.name}'.replace(' ', '_')
        return os.path.join(self.folder, name)

    @property
    def audio_file(self):
        """return temp file name including path"""
        name = f'audio_for_{self.name}'.replace(' ', '_')
        return os.path.join(self.folder, name)

    @property
    def temp_folder(self):
        return f'{self.temp_file}_parts_'

    @property
    def i(self):
        # This is where we put the animation letter
        if self.sched:
            selected_image = self.sched_string
        else:
            icon_list = self.animation_icons.get(self.status, [''])

            if time.time() - self.animation_timer > 0.5:
                self.animation_timer = time.time()
                self.animation_index += 1

            if self.animation_index >= len(icon_list):
                self.animation_index = 0

            selected_image = icon_list[self.animation_index]

        return selected_image

    @property
    def segment_size(self):
        return self._segment_size

    @segment_size.setter
    def segment_size(self, value):
        self._segment_size = value if value <= self.size else self.size
        # print('segment size = ', self._segment_size)

    @property
    def sched_string(self):
        # t = time.localtime(self.sched)
        # text = f"⏳({t.tm_hour}:{t.tm_min})"
        text = f"{self.sched[0]:02}:{self.sched[1]:02}"
        # text = f"⏳{self.sched[0]:02}:{self.sched[1]:02}"
        return text

    def kill_subprocess(self):
        """it will kill any subprocess running for this download item, ex: ffmpeg merge video/audio"""
        try:
            # to work subprocess should have shell=False
            self.subprocess.kill()
            log('run_command()> cancelled', self.subprocess.args)
            self.subprocess = None
        except Exception as e:
            log('DownloadItem.kill_subprocess()> error', e)

    def update(self, url):
        """get headers and update properties (eff_url, name, ext, size, type, resumable, status code/description)"""

        if url in ('', None):
            return

        headers = get_headers(url)
        # print('update d parameters:', headers)

        # update headers only if no other update thread created with different url
        if url == self.url:
            # print('update()> url, self.url', url, self.url)
            self.eff_url = headers.get('eff_url')
            self.status_code = headers.get('status_code', '')
            self.status_code_description = f"{self.status_code} - {translate_server_code(self.status_code)}"

            # get file name
            name = ''
            if 'content-disposition' in headers:  # example content-disposition : attachment; filename=ffmpeg.zip
                try:
                    name = headers['content-disposition'].split('=')[1].strip('"')
                except:
                    pass

            elif 'file-name' in headers:
                name = headers['file-name']
            else:
                clean_url = url.split('?')[0] if '?' in url else url
                name = clean_url.split('/')[-1].strip()

            # file size
            size = int(headers.get('content-length', 0))

            # type
            content_type = headers.get('content-type', '').split(';')[0]
            # fallback, guess type from file name extension
            # guessed_content_type = mimetypes.guess_type(name, strict=False)[0]
            # if not content_type:
            #     content_type = guessed_content_type

            # file extension:
            ext = os.path.splitext(name)[1]
            if not ext:  # if no ext in file name
                ext = mimetypes.guess_extension(content_type, strict=False) if content_type not in ('N/A', None) else ''

                if ext:
                    name += ext

            # resume support
            resumable = headers.get('accept-ranges', 'none') != 'none'

            self.name = name
            self.ext = ext
            self.size = size
            self.type = content_type
            self.resumable = resumable

            # reset segments
            self.segments.clear()
        else:
            print('DownloadItem.Update()> url changed, abort update for ', url)

        log('headers:', headers, log_level=3)

    def __repr__(self):
        return f'DownloadItem object( name: {self.name}, url:{self.url}'

    def delete_tempfiles(self, force_delete=False):
        """delete temp files and folder for a given download item"""

        if force_delete or not config.keep_temp:
            delete_folder(self.temp_folder)
            delete_file(self.temp_file)
            delete_file(self.audio_file)

    def save_progress_info(self):
        """save segments info to disk"""
        progress_info = [{'name': seg.name, 'downloaded': seg.downloaded, 'completed': seg.completed, 'size': seg.size}
                         for seg in self.segments]
        file = os.path.join(self.temp_folder, 'progress_info.txt')
        save_json(file, progress_info)

    def load_progress_info(self):
        """
        load progress info from disk, update segments' info, verify actual segments' size on disk
        :return: None
        """
        progress_info = None

        file = os.path.join(self.temp_folder, 'progress_info.txt')
        if os.path.isfile(file):
            # load progress info from temp folder if exist
            data = load_json(file)
            if isinstance(data, list):
                progress_info = data

        # update segments from progress info
        if progress_info:
            downloaded = 0

            # verify progress info
            for item in progress_info:
                # reset flags
                item['downloaded'] = False
                item['completed'] = False

                try:
                    size_on_disk = os.path.getsize(item.get('name'))
                    downloaded += size_on_disk
                    if size_on_disk == item.get('size'):
                        item['downloaded'] = True
                except:
                    continue

            # update segments
            if self.segments:
                for seg, item in zip(self.segments, progress_info):
                    if seg.name == item.get('name'):
                        seg.size = item.get('size') or seg.size
                        seg.downloaded = item.get('downloaded', False)
                        seg.completed = item.get('completed', False)

            # update self.downloaded
            self.downloaded = downloaded

    def reset_segments(self):
        """reset each segment properties "downloaded and merged" """
        for seg in self._segments:
            seg.downloaded = False
            seg.completed = False

    def prepare_for_downloading(self):
        """
        prepare download item for downloading, mainly for resume downloading
        :return: None
        """

        # first we will remove temp files because file manager is appending segments blindly to temp file
        delete_file(self.temp_file)
        delete_file(self.audio_file)

        # reset downloaded
        self.downloaded = 0

        # reset completed flag
        for seg in self.segments:
            seg.completed = False
            seg.downloaded = False

        # load progress info, verify actual segments' size, and update self.segments
        self.load_progress_info()

        # delete any previous encryption keys if segment is not completed to get a fresh key from server
        uncompleted = [seg for seg in self.segments if not seg.downloaded]
        for seg in uncompleted:
            if seg.key:
                seg.key.downloaded = False
                seg.key.completed = False
                delete_file(seg.key.name)






