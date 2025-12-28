#!/usr/bin/env python3
#
# Written 2017, 2019, 2023 by Tobias Brink
#
# To the extent possible under law, the author(s) have dedicated
# all copyright and related and neighboring rights to this software
# to the public domain worldwide. This software is distributed
# without any warranty.
#
# You should have received a copy of the CC0 Public Domain
# Dedication along with this software. If not, see
# <http://creativecommons.org/publicdomain/zero/1.0/>.

# Most of this code was used from this blog post:
# https://tbrink.science/blog/2017/04/30/processing-the-output-of-a-subprocess-with-python-in-realtime
# I don't understand why python is so terrible at dealing with outputs from usb_boot, but this works so ayyy

import errno
import os
import sys
import pty
import select
import signal
import subprocess
import time
from threading import Thread
from pathlib import Path

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

# Set signal handler for SIGINT.
signal.signal(signal.SIGINT, lambda s,f: print("received SIGINT"))

class OutStream:
    def __init__(self, fileno):
        self._fileno = fileno
        self._buffer = b""

    def read_lines(self):
        try:
            output = os.read(self._fileno, 1000)
        except OSError as e:
            if e.errno != errno.EIO: raise
            output = b""
        lines = output.split(b"\n")
        lines[0] = self._buffer + lines[0] # prepend previous
                                           # non-finished line.
        if output:
            self._buffer = lines[-1]
            finished_lines = lines[:-1]
            readable = True
        else:
            self._buffer = b""
            if len(lines) == 1 and not lines[0]:
                # We did not have buffer left, so no output at all.
                lines = []
            finished_lines = lines
            readable = False
            os.close(self._fileno)

        parsedLines = []
        for line in finished_lines:
            try:
                parsedLines.append(line.rstrip(b"\r").decode())
            except Exception:
                continue
        return parsedLines, readable

    def fileno(self):
        return self._fileno
    
class Command:

    def __init__(self, cmd, log):
        self.cmd = cmd
        self.streams = None
        self.process = None
        self.lines = []
        self.log = log

        self.out_r, self.out_w = pty.openpty()
        self.err_r, self.err_w = pty.openpty()

    def start(self):
        # Start the subprocess.
        self.process = subprocess.Popen(self.cmd, stdout=self.out_w, stderr=self.err_w)
        os.close(self.out_w) # if we do not write to process, close these.
        os.close(self.err_w)
        self.streams = {OutStream(self.out_r), OutStream(self.err_r)}
        self.thread = Thread(target=self.logToBuffer)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        if self.process:
            self.process.kill()

    def wait(self):
        while self.streams:
            time.sleep(0.1)

    def waitForLine(self, string: str, timeout: int = 0):
        while self.streams:
            for line in self.lines:
                if string in line:
                    return True
        return False
    
    def logToBuffer(self):
        # Log to sys.stdout.buffer
        while self.streams:
            lines = self.fetchStream()
            if not self.log:
                continue

            for line in lines:
                if line:
                    line = f"{bcolors.WARNING}{line}{bcolors.ENDC}\n"
                    sys.stdout.buffer.write(line.encode())
                    sys.stdout.buffer.flush()


    def fetchStream(self):
        if not self.streams:
            return
        
        outputLines = []
        
        while True:
            try:
                rlist, _, _ = select.select(self.streams, [], [])
                break
            except InterruptedError:
                continue
        # Handle all file descriptors that are ready.
        for f in rlist:
            lines, readable = f.read_lines()
            outputLines.extend(lines)
            # Example: Just print every line. Add your real code here.
            #for line in lines:
            #    print(line)
            if not readable:
                # This OutStream is finished.
                self.streams.remove(f)
        self.lines.extend(outputLines)
        return outputLines
