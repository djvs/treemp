#!/usr/bin/env python3
HELP_MESSAGE = """
treemp.py - Recursive ncurses audio/video player using libav/ffmpeg and python soundcard

Dependencies:
    pip install av soundcard numpy mutagen

Usage:
    ./treemp.py [file_or_directory ...]

Keys:
    Up/Down / j/k   : move selection
    PageUp/PageDn   : move selection by a page
    Right arrow     : expand directory node
    Left arrow      : collapse directory node
    Enter           : play selected file / toggle directory expansion
    Space / p       : pause/resume
    = / +           : volume up
    -               : volume down
    /               : filter list (Esc to clear, Enter to apply)
    r               : rescan all top-level paths
    q               : quit
    
Mouse:
    Left click line : Select file/folder line entry
    Right click line: Expand folder or play file immediately
    Left click bar  : Seek inside progress layout area
"""

import curses
import os
import sys
import threading
import time

try:
    import av
except ImportError:
    print("Missing dependency 'av'. Install with: pip install av")
    sys.exit(1)

try:
    import soundcard as sc
except ImportError:
    print("Missing dependency 'soundcard'. Install with: pip install soundcard")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("Missing dependency 'numpy'. Install with: pip install numpy")
    sys.exit(1)

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None


AUDIO_EXT = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".ape", ".alac", ".aiff"}
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".wmv", ".m4v", ".mpg", ".mpeg"}
PLAYABLE_EXT = AUDIO_EXT | VIDEO_EXT

SAMPLE_RATE = 48000
CHANNELS = 2
SCROLL_AMT = 8

def fmt_time(seconds):
    if seconds is None or seconds < 0 or seconds != seconds:
        seconds = 0
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class Node:
    """Represents a filesystem entry (file or folder) in our tree view."""
    def __init__(self, name, path, is_dir, depth, parent=None):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.depth = depth
        self.parent = parent
        self.children = []
        self.expanded = False
        self.loaded = False

    def toggle(self):
        if not self.is_dir:
            return

        if not self.loaded:
            self.load_children()

        self.expanded = not self.expanded

    def load_children(self):
        if not self.is_dir or self.loaded:
            return

        try:
            entries = list(os.scandir(self.path))
            entries.sort(key=lambda e: e.name.lower())

            children = []

            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        child = Node(
                            entry.name,
                            entry.path,
                            True,
                            self.depth + 1,
                            parent=self,
                        )
                        children.append(child)

                    elif (
                        entry.is_file(follow_symlinks=False)
                        and os.path.splitext(entry.name)[1].lower()
                        in PLAYABLE_EXT
                    ):
                        child = Node(
                            entry.name,
                            entry.path,
                            False,
                            self.depth + 1,
                            parent=self,
                        )
                        children.append(child)

                except OSError:
                    continue

            self.children = children

        except OSError:
            self.children = []

        self.loaded = True


class Player:
    """Decodes audio via PyAV and pushes directly to soundcard context wrappers."""

    def __init__(self, on_finished_callback=None):
        self.container = None
        self.audio_stream = None
        self.resampler = None
        self.speaker = None
        self.out_stream = None
        self.decode_thread = None
        self.stop_flag = threading.Event()
        self.on_finished_callback = on_finished_callback

        self.playing = False
        self.paused = False
        self.finished = False
        self.duration = 0.0
        self.position = 0.0
        self.volume = 1.0
        self.current_path = None

        # tracking targets for inline seeking
        self._seek_target = None
        self._lock = threading.Lock()

    def toggle_pause(self):
        if self.playing:
            self.paused = not self.paused

    def seek(self, delta_seconds):
        if not self.container:
            return
        with self._lock:
            # calculate absolute target time safely
            target = self.position + delta_seconds
            target = max(0.0, min(self.duration if self.duration else target, target))
            
            # simply flag the background thread to intercept and execute this target
            self._seek_target = target

    def set_volume(self, delta):
        self.volume = max(0.0, min(2.0, self.volume + delta))

    def play_file(self, path):
        self.stop()
        with self._lock:
            try:
                self.container = av.open(path)
            except Exception as e:
                self.current_path = None
                raise RuntimeError(f"Could not open file: {e}")

            audio_streams = [s for s in self.container.streams if s.type == "audio"]
            if not audio_streams:
                self.container.close()
                self.container = None
                raise RuntimeError("No audio stream found in file")

            self.audio_stream = audio_streams[0]
            self.current_path = path

            if self.container.duration:
                self.duration = float(self.container.duration) / av.time_base
            elif self.audio_stream.duration:
                self.duration = float(self.audio_stream.duration * self.audio_stream.time_base)
            else:
                self.duration = 0.0

            self.resampler = av.AudioResampler(format="flt", layout="stereo", rate=SAMPLE_RATE)
            self.position = 0.0
            self.finished = False
            self.stop_flag.clear()
            self._seek_target = None

            self.decode_thread = threading.Thread(target=self._decode_loop, daemon=True)
            self.decode_thread.start()

            self.playing = True
            self.paused = False

    def stop(self):
        self.stop_flag.set()
        
        if self.decode_thread and self.decode_thread.is_alive():
            self.decode_thread.join(timeout=1.0) 
        self.decode_thread = None
        
        with self._lock:
            if self.container:
                try:
                    self.container.close()
                except Exception:
                    pass
                self.container = None
            
        self.playing = False
        self.paused = False
        self.finished = False

    def _decode_loop(self):
        local_stream_sc = None
        try:
            with self._lock:
                local_stream = self.audio_stream
                local_container = self.container

            if not local_stream or not local_container:
                return

            speaker = sc.default_speaker()
            local_stream_sc = speaker.player(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=512)
            
            with local_stream_sc:
                self.out_stream = local_stream_sc 
                
                # fetch demux packet generator dynamically
                packets = local_container.demux(local_stream)
                
                while not self.stop_flag.is_set():
                    # SAFE INLINE SEEK HANDLING 
                    # check if a seek was requested by the UI thread
                    if self._seek_target is not None:
                        with self._lock:
                            target = self._seek_target
                            self._seek_target = None
                        
                        try:
                            offset = int(target / local_stream.time_base)
                            local_container.seek(offset, any_frame=False, backward=True, stream=local_stream)
                            
                            # re-instantiate the resampler graph to clear stale presentation timestamps
                            self.resampler = av.AudioResampler(format="flt", layout="stereo", rate=SAMPLE_RATE)
                            self.position = target
                            
                            # refresh the packet iteration reference stream
                            packets = local_container.demux(local_stream)
                        except Exception as e:
                            debug_print(f"Inline seek system exception: {e}")
                            continue

                    try:
                        packet = next(packets)
                    except StopIteration:
                        break # File finished naturally
                    except Exception:
                        continue

                    try:
                        frames = packet.decode()
                    except Exception:
                        continue
                        
                    for frame in frames:
                        if self.stop_flag.is_set() or self._seek_target is not None:
                            break
                        try:
                            with self._lock:
                                local_resampler = self.resampler
                            out_frames = local_resampler.resample(frame)
                        except Exception:
                            continue
                        if out_frames is None:
                            continue
                        if not isinstance(out_frames, list):
                            out_frames = [out_frames]
                        for f in out_frames:
                            if self.stop_flag.is_set() or self._seek_target is not None:
                                break
                                
                            arr = f.to_ndarray()
                            pcm = arr.reshape(-1, CHANNELS).astype(np.float32, order='C')
                            
                            self.position += len(pcm) / SAMPLE_RATE
                            if self.duration and self.position > self.duration:
                                self.position = self.duration
                            
                            while self.paused and not self.stop_flag.is_set() and self._seek_target is None:
                                time.sleep(0.005)

                            if self.stop_flag.is_set() or self._seek_target is not None:
                                break

                            local_stream_sc.play(pcm * self.volume)
                            
                # check if we broke out because the file genuinely reached its final frame
                if not self.stop_flag.is_set() and self._seek_target is None:
                    self.finished = True
                    if self.on_finished_callback:
                        self.on_finished_callback()
                        
        except Exception as e:
            debug_print(f"Decoding thread exception: {e}")
            self.finished = True
        finally:
            self.out_stream = None



def get_metadata(path):
    info = []
    ext = os.path.splitext(path)[1].lower()
    tags = {}
    techinfo = None

    if MutagenFile is not None:
        try:
            mf = MutagenFile(path, easy=True)
            if mf is not None:
                tags = dict(mf.tags) if mf.tags else {}
                techinfo = mf.info
        except Exception:
            pass

    def tag(*keys):
        for k in keys:
            if k in tags and tags[k]:
                v = tags[k]
                return v if isinstance(v, list) else v
        return None

    title = tag("title")
    artist = tag("artist")
    album = tag("album")
    date = tag("date", "year")
    genre = tag("genre")
    track = tag("tracknumber")
    albumartist = tag("albumartist")

    if title: info.append(("Title", str(title)))
    if artist: info.append(("Artist", str(artist)))
    if albumartist and albumartist != artist: info.append(("Album artist", str(albumartist)))
    if album: info.append(("Album", str(album)))
    if date: info.append(("Date", str(date)))
    if genre: info.append(("Genre", str(genre)))
    if track: info.append(("Track", str(track)))

    if techinfo is not None:
        if hasattr(techinfo, "length") and techinfo.length:
            info.append(("Duration", fmt_time(techinfo.length)))
        if hasattr(techinfo, "bitrate") and techinfo.bitrate:
            info.append(("Bitrate", f"{int(techinfo.bitrate / 1000)} kbps"))
        if hasattr(techinfo, "sample_rate") and techinfo.sample_rate:
            info.append(("Sample rate", f"{techinfo.sample_rate} Hz"))
        if hasattr(techinfo, "channels") and techinfo.channels:
            info.append(("Channels", str(techinfo.channels)))
        if hasattr(techinfo, "codec") and techinfo.codec:
            info.append(("Codec", str(techinfo.codec)))

    if not info or ext in VIDEO_EXT:
        try:
            c = av.open(path)
            meta = c.metadata or {}
            for k in ("title", "artist", "album", "genre"):
                if k in meta and not any(l.lower() == k for l, _ in info):
                    info.append((k.capitalize(), meta[k]))
            for s in c.streams:
                if s.type == "audio":
                    if not any(l == "Codec" for l, _ in info): info.append(("Codec", s.codec_context.name))
                    if not any(l == "Sample rate" for l, _ in info) and s.codec_context.sample_rate:
                        info.append(("Sample rate", f"{s.codec_context.sample_rate} Hz"))
                    if not any(l == "Channels" for l, _ in info) and s.codec_context.channels:
                        info.append(("Channels", str(s.codec_context.channels)))
                    if not any(l == "Duration" for l, _ in info) and c.duration:
                        info.append(("Duration", fmt_time(float(c.duration) / av.time_base)))
                    break
            c.close()
        except Exception:
            pass

    try:
        size = os.path.getsize(path)
        info.append(("File size", f"{size / (1024*1024):.2f} MB"))
    except Exception:
        pass

    info.append(("Format", ext.lstrip(".").upper()))
    return info

def build_tree(root_path):
    is_dir = os.path.isdir(root_path)

    root_node = Node(
        os.path.basename(root_path) or root_path,
        root_path,
        is_dir,
        0,
    )

    if not is_dir:
        return root_node

    # load only the top-level directory
    root_node.load_children()
    root_node.expanded = True

    return root_node

class App:
    def __init__(self, stdscr, roots):
        self.stdscr = stdscr
        self.stdscr.keypad(True)

        self.root_nodes = roots
        self.player = Player(on_finished_callback=self.handle_track_finished)
        self.visible_nodes = []

        self.sel = 0
        self.scroll = 0
        self.search_mode = False
        self.search_text = ""
        self.status_msg = ""
        self.status_msg_time = 0

        self.metadata_path = None
        self.metadata = []
        self.status_metadata = ""

        self.auto_advance_queued = False
        self.metadata_visible = True
        self.search_text = ""
        self.tree_dirty = True
        self.flatten_tree()

        self._spawn_lock = threading.Lock()

        curses.curs_set(0)
        self.stdscr.nodelay(True)
        curses.mouseinterval(0)
        
        mask = (
            curses.BUTTON1_PRESSED |
            curses.BUTTON1_RELEASED |
            curses.BUTTON3_PRESSED |
            curses.BUTTON3_RELEASED | 
            curses.BUTTON4_PRESSED | 
            curses.BUTTON5_PRESSED
        )

        curses.mousemask(mask)

        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_GREEN)
        curses.init_pair(6, curses.COLOR_CYAN, -1)
        curses.init_pair(7, curses.COLOR_GREEN, -1)
        curses.init_pair(8, curses.COLOR_BLACK, curses.COLOR_GREEN)

        self.nav_win = None
        self.meta_win = None
        self.status_win = None

        self.compute_layout()

    def flatten_tree(self):
        if not self.tree_dirty:
            return

        self.visible_nodes = []

        def _walk(node):
            self.visible_nodes.append(node)

            if node.is_dir and node.expanded:
                for child in node.children:
                    _walk(child)

        for root in self.root_nodes:
            _walk(root)

        self.tree_dirty = False

    def search_next(self, text):
        if not text or not self.visible_nodes:
            return

        text = text.casefold()
        count = len(self.visible_nodes)

        # start immediately after current selection
        for offset in range(1, count + 1):
            index = (self.sel + offset) % count
            node = self.visible_nodes[index]

            if text in node.name.casefold():
                self.sel = index

                # keep the match visible

                if text in node.name.casefold():
                    self.sel = index

                    visible_height = max(1, self.nav_h - 2)

                    # center the match in the navigation pane
                    self.scroll = self.sel - visible_height // 2

                    max_scroll = max(0, len(self.visible_nodes) - visible_height)
                    self.scroll = min(max(0, self.scroll), max_scroll)

                    self.set_status(f"Found: {node.name}")
                    return

                self.set_status(f"Found: {node.name}")
                return

        self.set_status(f"Not found: {text}")

    def load_metadata(self, path):
        if not path:
            self.metadata_path = None
            self.metadata = []
            self.status_metadata = ""
            return

        try:
            metadata = get_metadata(path)
        except Exception as e:
            metadata = [("Error", str(e))]

        self.metadata_path = path
        self.metadata = metadata

        parts = []
        meta_dict = dict(metadata)

        artist = meta_dict.get("Artist")
        title = meta_dict.get("Title")
        album = meta_dict.get("Album")

        if artist:
            parts.append(str(artist))
        if title:
            parts.append(str(title))
        if album:
            parts.append(str(album))

        self.status_metadata = " — ".join(parts)

    def compute_layout(self):
        h, w = self.stdscr.getmaxyx()
        self.h, self.w = h, w

        self.status_h = 6
        self.nav_h = max(3, int(h - self.status_h))
        self.status_y = self.nav_h
        #self.status_h = h - self.nav_h

        if self.status_h < 3:
            self.status_h = 3
            self.nav_h = h - self.status_h
            self.status_y = self.nav_h

        if self.metadata_visible:
            self.nav_w = max(20, int(w * 0.75))
            self.meta_w = w - self.nav_w
        else:
            self.nav_w = w
            self.meta_w = 0

        self.meta_h = self.nav_h

        self.nav_win = curses.newwin(
            self.nav_h, self.nav_w, 0, 0
        )

        if self.metadata_visible:
            self.meta_win = curses.newwin(
                self.meta_h, self.meta_w, 0, self.nav_w
            )
        else:
            self.meta_win = None

        self.status_win = curses.newwin(
            self.status_h, self.w, self.status_y, 0
        )

    def set_status(self, msg):
        self.status_msg = msg
        self.status_msg_time = time.time()

    def handle_track_finished(self):
        self.auto_advance_queued = True

    def check_auto_advance(self):
        if self.auto_advance_queued:
            self.auto_advance_queued = False
            self.advance_track()

    def advance_track(self):
        self.next_track()

    def get_playable_files(self):
        files = []

        def _collect(node):
            if not node.is_dir:
                files.append(node.path)
            for child in node.children:
                _collect(child)

        for root in self.root_nodes:
            _collect(root)

        return files

    def reveal_current_song(self):
        path = self.player.current_path

        if not path:
            return

        if self.reveal_path(path):
            node = self.visible_nodes[self.sel]
            self.set_status(f"Located: {node.name}")
        else:
            self.set_status("Could not locate playing song")

    def reveal_path(self, path):
        path = os.path.abspath(path)

        def same_path(a, b):
            return os.path.normcase(os.path.abspath(a)) == \
                   os.path.normcase(os.path.abspath(b))

        target = None

        for root in self.root_nodes:
            root_path = os.path.abspath(root.path)

            if same_path(root_path, path):
                target = root
                break

            if not root.is_dir:
                continue

            try:
                relative = os.path.relpath(path, root_path)
            except ValueError:
                continue

            if relative == os.pardir or relative.startswith(
                os.pardir + os.sep
            ):
                continue

            node = root

            for part in relative.split(os.sep):
                if not node.is_dir:
                    node = None
                    break

                if not node.loaded:
                    node.load_children()

                child = None
                for candidate in node.children:
                    if os.path.normcase(candidate.name) == os.path.normcase(part):
                        child = candidate
                        break

                if child is None:
                    node = None
                    break

                node.expanded = True
                node = child

            if node is not None and same_path(node.path, path):
                target = node
                break

        if target is None:
            return False

        self.tree_dirty = True
        self.flatten_tree()

        try:
            self.sel = self.visible_nodes.index(target)
        except ValueError:
            return False

        visible_height = max(1, self.nav_h - 2)

        self.scroll = max(
            0,
            self.sel - visible_height // 2
        )

        max_scroll = max(
            0,
            len(self.visible_nodes) - visible_height
        )

        self.scroll = min(self.scroll, max_scroll)

        return True

    def _find_sibling_track(self, current_path, direction="next"):
        # lazily searches for the next or previous track in the filesystem tree without scanning unvisited directories
        current_path = os.path.abspath(current_path)
        
        def _get_sorted_playable_entries(directory):
            try:
                with os.scandir(directory) as it:
                    entries = list(it)
                entries.sort(key=lambda e: e.name.lower())
                return [e for e in entries if e.is_dir(follow_symlinks=False) or 
                        (e.is_file(follow_symlinks=False) and os.path.splitext(e.name)[1].lower() in PLAYABLE_EXT)]
            except OSError:
                return []


        def _first_leaf(path):
            # fnds the absolute first playable file down a directory branch
            if os.path.isfile(path):
                return path
            entries = _get_sorted_playable_entries(path)
            for entry in entries:
                res = _first_leaf(entry.path)
                if res: return res
            return None

        def _last_leaf(path):
            # finds the absolute last playable file down a directory branch
            if os.path.isfile(path):
                return path
            entries = _get_sorted_playable_entries(path)
            for entry in reversed(entries):
                res = _last_leaf(entry.path)
                if res: return res
            return None

        # trace structural hierarchy upwards from the active song
        target_dir = os.path.dirname(current_path)
        current_item_name = os.path.basename(current_path)

        while True:
            siblings = _get_sorted_playable_entries(target_dir)
            sibling_names = [s.name.lower() for s in siblings]
            
            try:
                curr_idx = sibling_names.index(current_item_name.lower())
            except ValueError:
                curr_idx = -1

            if curr_idx != -1:
                if direction == "next":
                    # look at items after the current one in this directory
                    for s in siblings[curr_idx + 1:]:
                        res = _first_leaf(s.path)
                        if res: return res
                else:
                    # look at items before the current one in this directory
                    for s in reversed(siblings[:curr_idx]):
                        res = _last_leaf(s.path)
                        if res: return res

            # if we hit the top-level tree roots, switch between top roots
            root_paths = [os.path.abspath(r.path) for r in self.root_nodes]
            if target_dir in root_paths:
                r_idx = root_paths.index(target_dir)
                if direction == "next" and r_idx + 1 < len(root_paths):
                    return _first_leaf(root_paths[r_idx + 1])
                elif direction == "previous" and r_idx > 0:
                    return _last_leaf(root_paths[r_idx - 1])
                break # reached the absolute end or beginning of the library list

            # step up one level higher into the parent directory and repeat
            current_item_name = os.path.basename(target_dir)
            target_dir = os.path.dirname(target_dir)

        return None

    def previous_track(self):
        current_path = self.player.current_path
        if not current_path:
            return

        path = self._find_sibling_track(current_path, direction="previous")
        if path:
            self.reveal_path(path)
            self._async_play_worker(path, f"Playing {os.path.basename(path)}")

    def next_track(self):
        current_path = self.player.current_path
        if not current_path:
            return

        path = self._find_sibling_track(current_path, direction="next")
        if path:
            self.reveal_path(path)
            self._async_play_worker(path, f"Playing {os.path.basename(path)}")

    def draw_nav(self):
        win = self.nav_win
        win.erase()
        win.box()
        title = f" Playlist ({len(self.visible_nodes)} items) "
        win.addnstr(0, 2, title, self.nav_w - 4, curses.color_pair(2) | curses.A_BOLD)

        list_h = self.nav_h - 2
        if self.sel < self.scroll:
            self.scroll = self.sel
        elif self.sel >= self.scroll + list_h:
            self.scroll = self.sel - list_h + 1

        for i in range(list_h):
            idx = self.scroll + i
            if idx >= len(self.visible_nodes):
                break
            node = self.visible_nodes[idx]
            is_playing = self.player.current_path == node.path
            
            indent = "  " * node.depth
            prefix = "[+] " if node.is_dir and not node.expanded else "[-] " if node.is_dir else "  "
            label = indent + prefix + node.name
            
            if len(label) > self.nav_w - 4:
                label = label[:self.nav_w - 7] + "..."
                
            attr = 0
            if idx == self.sel:
                if node.is_dir:
                    attr = curses.color_pair(1) | curses.A_BOLD
                else:
                    if is_playing:
                        attr = curses.color_pair(8)
                    else:
                        attr = curses.color_pair(1)

            elif node.is_dir:
                attr = curses.color_pair(6) | curses.A_BOLD
            elif is_playing:
                attr = curses.color_pair(3) | curses.A_BOLD
                
            try:
                win.addnstr(1 + i, 1, label.ljust(self.nav_w - 2), self.nav_w - 2, attr)
            except curses.error:
                pass

        if self.search_mode:
            prompt = f"/{self.search_text}"
            win.addnstr(
                self.nav_h - 1,
                1,
                prompt.ljust(self.nav_w - 2),
                self.nav_w - 2,
                curses.A_REVERSE
            )

        win.noutrefresh()

    def draw_meta(self):
        if not self.metadata_visible or self.meta_win is None:
            return

        win = self.meta_win
        win.erase()
        win.box()
        win.addnstr(0, 2, " Metadata ", self.meta_w - 4, curses.color_pair(2) | curses.A_BOLD)

        path = self.player.current_path
        row = 2
        inner_w = self.meta_w - 4
        if not path:
            win.addnstr(row, 2, "No file playing.", inner_w)
        else:
            fname = os.path.basename(path)
            win.addnstr(row, 2, fname[:inner_w], inner_w, curses.A_BOLD)
            row += 2
            meta = self.metadata if self.metadata_path == path else []

            for label, value in meta:
                if row >= self.meta_h - 1:
                    break
                label_text = f"{label}: "
                win.move(row, 2)
                win.addnstr(label_text[:inner_w], inner_w, curses.color_pair(2))
                rem_w = inner_w - len(label_text)
                if rem_w > 0:
                    win.addnstr(value[:rem_w], rem_w, curses.color_pair(0) | curses.A_BOLD)
                    value = value[rem_w:]
                row += 1
                while len(value) > 0 and row < self.meta_h - 1:
                    win.move(row, 2)
                    # indent addtl lines
                    win.addnstr("  ", inner_w, curses.color_pair(0) | curses.A_BOLD)
                    wrap_w = inner_w - 2 
                    win.addnstr(value[:wrap_w], wrap_w, curses.color_pair(0) | curses.A_BOLD)
                    value = value[wrap_w:]
                    row += 1
        win.noutrefresh()

    def draw_status(self):
        win = self.status_win
        win.erase()
        win.box()

        p = self.player
        pos = p.position
        dur = p.duration
        pct = (pos / dur * 100) if dur else 0.0

        state = "Stopped"
        if p.playing:
            state = "Paused" if p.paused else "Playing"
        if p.playing and p.finished:
            state = "Finished"

        name = os.path.basename(p.current_path) if p.current_path else "(none)"

        header = f" {state}  |  {name}  |  Vol {int(p.volume*100)}% "
        win.addnstr(0, 2, header[:self.w - 4], self.w - 4, curses.A_BOLD)

        metadata_line = self.status_metadata

        if metadata_line and self.status_h > 1:
            win.addnstr(
                1,
                2,
                metadata_line[:self.w - 4],
                self.w - 4,
                curses.color_pair(2) | curses.A_BOLD
            )

        self.bar_start_x = 2
        self.bar_width = max(1, self.w - 4)
        self.bar_y = self.status_y + 2

        filled = int(self.bar_width * (pct / 100.0)) if dur else 0

        if self.status_h > 2:
            if filled > 0:
                win.addnstr(
                    2, 2,
                    "-" * filled,
                    filled,
                    curses.color_pair(5)
                )

            remaining = self.bar_width - filled
            if remaining > 0:
                win.addnstr(
                    2, 2 + filled,
                    "-" * remaining,
                    remaining,
                    curses.color_pair(7)
                )

        info = f"{fmt_time(pos)} / {fmt_time(dur)}  ({pct:5.1f}%)"
        if self.status_h > 3:
            win.addnstr(3, 2, info[:self.w - 4], self.w - 4)

        if self.status_msg and time.time() - self.status_msg_time < 3 and self.status_h > 3:
            win.addnstr(
                3,
                max(2, self.w - len(self.status_msg) - 3),
                self.status_msg,
                len(self.status_msg),
                curses.A_DIM
            )

        if self.status_h > 4:
            help_items = [
                ("Arrows", "Nav/Expand"),
                ("Enter", "Play"),
                ("</>", "Prev/Next"),
                ("Space/p", "Pause"),
                ("+/= / -", "Vol"),
                ("Backsp", "Restart track"),
                ("/", "Search"),
                ("o", "Show current song"),
                ("m", "Hide meta"),
                ("q", "Quit"),
            ]

            x = 2
            max_x = self.w - 2

            for key, description in help_items:
                key_text = key
                desc_text = f": {description}  "

                if x + len(key_text) + len(desc_text) > max_x:
                    break

                win.addnstr(
                    4, x,
                    key_text,
                    len(key_text),
                    curses.A_BOLD
                )
                x += len(key_text)

                win.addnstr(
                    4, x,
                    desc_text,
                    len(desc_text),
                    curses.A_DIM
                )
                x += len(desc_text)

        win.noutrefresh()

    def draw(self):
        self.draw_nav()
        self.draw_meta()
        self.draw_status()
        curses.doupdate()

    def _async_play_worker(self, path, status_on_success):
        def _task():
            # acquire the lock to ensure only one thread setup happens at an exact instant
            if not self._spawn_lock.acquire(blocking=False):
                # if a thread setup is already in progress, gracefully drop this overlapped request
                return
            try:
                self.player.play_file(path)
                self.load_metadata(path)
                self.set_status(status_on_success)
            except Exception as e:
                self.set_status(f"Error: {e}")
            finally:
                self._spawn_lock.release()

        threading.Thread(target=_task, daemon=True).start()

    def handle_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return

        # mouse wheel
        if bstate & curses.BUTTON4_PRESSED:
            self.sel = max(0, self.sel - SCROLL_AMT)
            self.scroll = max(0, self.scroll - SCROLL_AMT)
            return

        if bstate & curses.BUTTON5_PRESSED:
            max_sel = max(0, len(self.visible_nodes) - 1)
            self.sel = min(max_sel, self.sel + SCROLL_AMT)
            self.scroll = min(
                max(0, len(self.visible_nodes) - self.nav_h + 1),
                self.scroll + SCROLL_AMT
            )
            return

        # left button: select
        if bstate & curses.BUTTON1_PRESSED:
            if 0 < my < self.nav_h - 1 and 0 < mx < self.nav_w - 1:
                clicked_idx = self.scroll + (my - 1)

                if 0 <= clicked_idx < len(self.visible_nodes):
                    self.sel = clicked_idx
            elif my == self.bar_y:
                if self.bar_start_x <= mx < self.bar_start_x + self.bar_width:
                    if self.player.duration > 0:
                        clicked_pct = (
                            (mx - self.bar_start_x) / self.bar_width
                        )
                        target_time = clicked_pct * self.player.duration

                        self.player.seek(target_time - self.player.position)
                        self.set_status(f"Seeked to {fmt_time(target_time)}")
            return

        # right button: select + toggle/play
        if bstate & curses.BUTTON3_PRESSED:
            if 0 < my < self.nav_h - 1 and 0 < mx < self.nav_w - 1:
                clicked_idx = self.scroll + (my - 1)

                if 0 <= clicked_idx < len(self.visible_nodes):
                    node = self.visible_nodes[clicked_idx]
                    self.sel = clicked_idx

                    if node.is_dir:
                        node.toggle()
                        self.tree_dirty = True
                        self.flatten_tree()
                    else:
                        self._async_play_worker(
                            node.path,
                            f"Playing {node.name}"
                        )
            return


    def handle_key(self, ch):
        if self.search_mode:
            if ch in (curses.KEY_ENTER, 10, 13):
                self.search_mode = False

                if self.search_text:
                    self.search_next(self.search_text)

            elif ch == 27:  # esc
                self.search_mode = False
                self.search_text = ""

            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.search_text = self.search_text[:-1]

            elif 32 <= ch <= 126:
                self.search_text += chr(ch)

            return


        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if self.player.current_path:
                self.player.seek(-self.player.position)
                self.set_status("Restarted track")

        elif ch in (ord("m"), ord("M")):
            self.metadata_visible = not self.metadata_visible
            self.compute_layout()

        elif ch in (ord("q"), ord("Q")):
            raise KeyboardInterrupt

        elif ch == ord("<"):
            self.previous_track()

        elif ch == ord(">"):
            self.next_track()

        elif ch == ord("/"):
            self.search_mode = True
            self.search_text = ""

        elif ch in (ord("n"), ord("N")):
            if self.search_text:
                self.search_next(self.search_text)

        elif ch in (ord("o"), ord("O")):
            self.reveal_current_song()

        elif ch in (curses.KEY_UP, ord("k")):
            self.sel = max(0, self.sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel = min(len(self.visible_nodes) - 1, self.sel + 1)
        
        elif ch == curses.KEY_LEFT:
            if self.visible_nodes:
                node = self.visible_nodes[self.sel]
                if node.is_dir and node.expanded:
                    node.expanded = False
                    self.tree_dirty = True
                    self.flatten_tree()
                elif node.parent:
                    if node.parent in self.visible_nodes:
                        self.sel = self.visible_nodes.index(node.parent)
                        
        elif ch == curses.KEY_RIGHT:
            if self.visible_nodes:
                node = self.visible_nodes[self.sel]

                if node.is_dir:
                    if not node.loaded:
                        node.load_children()

                    if not node.expanded:
                        node.expanded = True
                        self.tree_dirty = True
                        self.flatten_tree()
                    
        elif ch == curses.KEY_PPAGE:
            self.sel = max(0, self.sel - (self.nav_h - 2))
        elif ch == curses.KEY_NPAGE:
            self.sel = min(len(self.visible_nodes) - 1, self.sel + (self.nav_h - 2))
        elif ch in (curses.KEY_ENTER, 10, 13):
            if self.visible_nodes:
                node = self.visible_nodes[self.sel]
                if node.is_dir:
                    node.toggle()
                    self.tree_dirty = True
                    self.flatten_tree()
                else:
                    self._async_play_worker(node.path, f"Playing {node.name}")
        elif ch in (ord(" "), ord("p")):
            self.player.toggle_pause()
        elif ch in (ord("="), ord("+")):
            self.player.set_volume(0.05)
        elif ch == ord("-"):
            self.player.set_volume(-0.05)
        elif ch in (ord("r"), ord("R")):
            for root in self.root_nodes:
                if root.is_dir:
                    root.children = []
                    root.loaded = False
                    root.load_children()
                    root.expanded = True

            self.flatten_tree()
            self.set_status("Rescanned library structures")
        elif ch == curses.KEY_MOUSE:
            self.handle_mouse()
        elif ch == curses.KEY_RESIZE:
            self.compute_layout()
        elif ch == curses.KEY_HOME:
            self.sel = 0
            self.scroll = 0
            return

        elif ch == curses.KEY_END:
            self.sel = max(0, len(self.visible_nodes) - 1)
            self.scroll = max(0, len(self.visible_nodes) - self.nav_h + 1)
            return

    def run(self):
        try:
            next_draw = 0.0

            while True:
                self.check_auto_advance()

                now = time.monotonic()

                if now >= next_draw:
                    self.draw()
                    next_draw = now + 0.1  # 10 FPS

                ch = self.stdscr.getch()

                if ch == -1:
                    time.sleep(0.01)
                    continue

                if ch == curses.KEY_MOUSE:
                    self.handle_mouse()
                else:
                    self.handle_key(ch)

        except KeyboardInterrupt:
            pass
        finally:
            self.player.stop()

def debug_print(*args, **kwargs):
    with open("/tmp/mouse-debug", "a") as f:
        print(*args, file=f, **kwargs)

def main(stdscr, roots):
    app = App(stdscr, roots)
    app.run()

if __name__ == "__main__":
    if len(sys.argv) <= 1:
        print(HELP_MESSAGE)
        sys.exit(1)

    roots = []

    for arg in sys.argv[1:]:
        path = os.path.abspath(os.path.expanduser(arg))

        if os.path.isdir(path) or os.path.isfile(path):
            roots.append(build_tree(path))
        else:
            debug_print(f"Not found: {arg}")

    if not roots:
        print("No valid files or folders supplied")
        sys.exit(1)

    try:
        curses.wrapper(main, roots)
    except Exception as e:
        print(f"Fatal execution crash: {e}")
        sys.exit(1)
