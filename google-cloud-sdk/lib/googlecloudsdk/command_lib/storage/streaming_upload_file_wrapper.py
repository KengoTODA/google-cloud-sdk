# -*- coding: utf-8 -*- #
# Copyright 2022 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implements a file wrapper used for in-flight retries of streaming uploads."""

from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import collections
import os

from googlecloudsdk.command_lib.storage import errors


class StreamingUploadFileWrapper:
  """Supports limited seeking within a non-seekable stream by buffering data."""

  def __init__(self, stream, max_buffer_size):
    """Initializes the wrapper.

    Args:
      stream: The input stream.
      max_buffer_size: Maximum size of the internal buffer. This should be >= to
          the chunk size used by the API to execute streaming uploads to ensure
          that at least one full chunk write can be repeated in the event of a
          server error.
    """

    self._stream = stream

    self._buffer = collections.deque()
    self._max_buffer_size = max_buffer_size
    self._buffer_start = 0
    self._position = 0
    self._buffer_end = 0

  @property
  def mode(self):
    return getattr(self._stream, 'mode', None)

  def seekable(self):
    """Returns True, though seek support is limited."""
    return True

  def close(self):
    """Closes the wrapped stream."""
    return self._stream.close()

  def tell(self):
    """Returns the current position in the stream."""
    return self._position

  def _read_from_buffer(self, amount):
    """Get any buffered data required to complete a read.

    If a backward seek has not happened, the buffer will never contain any
    information needed to complete a read call. Return the empty string in
    these cases.

    If the current position is before the end of the buffer, some of the
    requested bytes will be in the buffer. For example, if our position is 1,
    five bytes are being read, and the buffer contains b'0123', we will return
    b'123'. Two additional bytes will be read from the stream at a later stage.

    Args:
      amount (int): The total number of bytes to be read

    Returns:
      A byte string, the length of which is equal to `amount` if there are
      enough buffered bytes to complete the read, or less than `amount` if there
      are not.
    """
    buffered_data = []
    bytes_remaining = amount
    if self._position < self._buffer_end:
      # There was a backward seek, so read from the buffer.
      position_in_buffer = self._buffer_start
      for data in self._buffer:
        if position_in_buffer + len(data) >= self._position:
          offset_from_position = self._position - position_in_buffer
          bytes_to_read_this_block = len(data) - offset_from_position
          read_size = min(bytes_to_read_this_block, bytes_remaining)
          buffered_data.append(data[offset_from_position:offset_from_position +
                                    read_size])
          bytes_remaining -= read_size
        position_in_buffer += len(data)
        self._position += read_size
    return b''.join(buffered_data)

  def _store_data(self, data):
    """Adds data to the buffer, respecting max_buffer_size.

    The buffer can consist of many different blocks of data, e.g.

      [b'0', b'12', b'3']

    With a maximum size of 4, if we read two bytes, we must discard the oldest
    data and keep half of the second-oldest block:

      [b'2', b'3', b'45']

    Args:
      data (bytes): the data being added to the buffer.
    """
    if data:
      self._buffer.append(data)
      self._buffer_end += len(data)
      oldest_data = None
      while self._buffer_end - self._buffer_start > self._max_buffer_size:
        oldest_data = self._buffer.popleft()
        self._buffer_start += len(oldest_data)
        if oldest_data:
          refill_amount = self._max_buffer_size - (
              self._buffer_end - self._buffer_start)
          if refill_amount >= 1:
            self._buffer.appendleft(oldest_data[-refill_amount:])
            self._buffer_start -= refill_amount

  def read(self, size=-1):
    """Reads from the wrapped stream.

    Args:
      size: The amount of bytes to read. If omitted or negative, the entire
          stream will be read and returned.

    Returns:
      Bytes from the wrapped stream.
    """
    read_all_bytes = size is None or size < 0
    if read_all_bytes:
      # Ensures everything is read from the buffer.
      bytes_remaining = self._max_buffer_size
    else:
      bytes_remaining = size

    data = self._read_from_buffer(bytes_remaining)
    bytes_remaining -= len(data)

    if read_all_bytes:
      new_data = self._stream.read(-1)
    elif bytes_remaining:
      new_data = self._stream.read(bytes_remaining)
    else:
      new_data = b''

    self._position += len(new_data)
    self._store_data(new_data)

    return data + new_data

  def seek(self, offset, whence=os.SEEK_SET):
    """Seeks within the buffered stream."""
    if whence == os.SEEK_SET:
      if offset < self._buffer_start or offset > self._buffer_end:
        raise errors.Error(
            'Unable to recover from an upload error because limited buffering'
            ' is available for streaming uploads. Offset {} was requested, but'
            ' only data from {} to {} is buffered.'.format(
                offset, self._buffer_start, self._buffer_end))
      self._position = offset
    elif whence == os.SEEK_END:
      if offset > self._max_buffer_size:
        raise errors.Error(
            'Invalid SEEK_END offset {} on streaming upload. Only {} bytes'
            ' can be buffered.'.format(offset, self._max_buffer_size))

      while self.read(self._max_buffer_size):
        pass
      self._position -= offset
    else:
      raise errors.Error(
          'Invalid seek mode on streaming upload. Mode: {}, offset: {}'.format(
              whence, offset))


