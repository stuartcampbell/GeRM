import asyncio
from asyncio import DatagramProtocol, get_event_loop
import zmq
import zmq.asyncio
import numpy as np
from enum import Enum
from collections import defaultdict
import time
import struct

from pygerm.client import event2payload


class ListenAndSend(DatagramProtocol):
    def __init__(self):
        super().__init__()
        self.armed = False
        self.target_addr = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        print(data, addr)
        try:
            sig, adr, enable = struct.unpack('!III', data)
        except ValueError:
            return
        if sig != 0xdeadbeef:
            return

        self.armed = bool(enable)
        if self.armed:
            self.target_addr = addr
        else:
            self.target_addr = None

        # echo back the correct thing
        # 'x' is a pad byte, xxxx is same size as I (4 byte unsigned)
        self.transport.sendto(struct.pack('!xxxxI', 0x4f6b6179), addr)

    def send(self, data):
        if not self.armed:
            return
        self.transport.sendto(data, (self.target_addr[0], 0x7D03))


class RegisterPort(DatagramProtocol):

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        print('MATE', 0x7D01)
        print(data, addr)
        self.transport.send(data, addr)


class CMDS(Enum):
    REG_READ = 0
    REG_WRITE = 1
    START_DMA = 2


FIFODATAREG = 24
FIFORDCNTREG = 25
FIFOCNTRLREG = 26

FRAMEACTIVEREG = 52
FRAMENUMREG = 54
FRAMELENREG = 53


ctx = zmq.asyncio.Context()
loop = zmq.asyncio.ZMQEventLoop()
asyncio.set_event_loop(loop)

# average number of events per msg
N = 500000
# number of messages
n_msgs = 50
# average total exposure
simulated_exposure = 10
# expected ticks between events
# ([S] / [event]) * ([tick] / [s]) = [tick] / [event]
tick_gap = int((simulated_exposure / N) / (40 * 10e-9))

n_chips = 12
n_chans = 32


def simulate_line(n, c):
    ''' simulate a line based on channel position c.
        data type is not assumed here, typecast when receiving it.

        This should create a curve that goes sinusoidally as the channel #.
        (But same for all chip numbers)
    '''
    chan = c % n_chans
    # chip = c // n_chans

    chan = chan.astype(float)
    return np.clip(
        (np.random.randn(len(chan)) * 100) + 1000 + 2000 * (chan / n_chans),
        0, 2**12 - 1)


def simulate_random(n):
    return np.random.randint(0, high=2**12 + 1, size=n, dtype=np.uint32)


def make_sim_payload(num, n_chips, n_chans, tick_gap, ts_offset):
    '''
       layout:
       "0" [[4 bit chip addr] [5 bit channel addr]] [10 bit TD] [12 bit PD]
       "1000" [28 bit time stamp]

        pix_id is chip_no*n_chans + chan_no
            and n_chans will be 2**(some value)
            (Im guessing)

        Here, n_chips is 12 which means values go from 0-11
            which is 0000 to 1011 in binary
        n_chans is 32 which means values go from 0-31
            which means 0000 to 1111

        max value is [1011] [1 1111] = 1 0111 1111
        this equals 383.

        NOTE : I choose some endianess here but it doesn't matter, it can be
        changed later on. We may receive a better performance between once
        versus the other, depending on what we send to etc.
        (Not sure about this)
    '''
    # the endianness doesn't matter so much yet, but size does
    payload = np.zeros(num*2, dtype="<u4")
    # the two words as a view in numpy
    # word1 = payload[::2]
    # word2 = payload[1::2]

    # choose a random pixel id
    # n_chips, n_chans are 12, 32 (or see var set above)
    #
    # this gives 383 (binary to number) Commenting out and hard coding
    # to be safe
    # print(0b101111111) gives 383
    MAX_ID = 383

    # for debugging, could change this to some other non-uniform function
    pix_id = np.random.randint(0, high=MAX_ID+1, size=num).astype('<u4')
    chip_id = pix_id // n_chans
    chan_id = pix_id % n_chans

    # fine timestamp
    td = np.random.randint(0, high=2**10+1, size=num, dtype='<u4')

    # energy
    # simulate a resonable looking energy by giving detector position
    # make it 4 byte integer
    pd = simulate_line(num, chip_id*n_chans + chan_id).astype('<u4')

    # coarse timestamp
    ts = np.mod((np.cumsum(
        np.random.poisson(tick_gap, size=num).astype('<u4')) +
                 ts_offset),
                2**31)

    payload = event2payload(chip_id, chan_id, td, pd, ts)

    return payload, ts[-1]


async def recv_and_process():
    loop = get_event_loop()
    responder = ctx.socket(zmq.REP)
    publisher = ctx.socket(zmq.PUB)
    responder.bind(b'tcp://*:5555')
    publisher.bind(b'tcp://*:5556')
    state = defaultdict(int)

    ts_offset = 0

    _, udp = await loop.create_datagram_endpoint(
        ListenAndSend, local_addr=('localhost', 0x7D00))

    # this is not actually used (yet)
    await loop.create_datagram_endpoint(
        RegisterPort, local_addr=('localhost', 0x7D01))

    async def sim_data():
        nonlocal ts_offset
        state[FRAMENUMREG] += 1
        num_per_msg = np.random.poisson(N, size=n_msgs)
        ts_offset = 0
        udp_packet_count = 0
        tail = []
        for num in num_per_msg:
            payload, ts_offset = make_sim_payload(num,
                                                  n_chips, n_chans,
                                                  tick_gap,
                                                  ts_offset)
            # use astype to ensure it's prepared for big endian (network order)
            payload = payload.astype('>u4')
            await publisher.send_multipart([b'data', payload])
            if udp_packet_count == 0:
                # special case packet 0
                tail = payload
                head, tail = tail[:1020], tail[1020:]
                # packets are 1024*4 bytes long total, first four unsigned ints
                # (4 bytes each) are here, rest is data
                header = struct.pack('!III',
                                     udp_packet_count,
                                     0xfeedface,
                                     state[FRAMENUMREG])
                udp.send(b''.join((header,
                                   bytes(head))))
                udp_packet_count += 1
            else:
                first_head = 1022 - len(tail)
                head = (tail, payload[:first_head])
                tail = payload[first_head:]
                header = struct.pack('!I', udp_packet_count)
                udp.send(b''.join((header,
                                   bytes(head[0]),
                                   bytes(head[1]))))
                udp_packet_count += 1

            while len(tail) > 1022:
                head, tail = tail[:1022], tail[1022:]
                header = struct.pack('!I', udp_packet_count)
                udp.send(b''.join((header, bytes(head))))
                udp_packet_count += 1
        header = struct.pack('!I', udp_packet_count)
        footer = struct.pack('!II', 0, 0xdecafbad)
        udp.send(b''.join((header,
                           bytes(tail),
                           footer)))

        # <u4 little endian 4 byte unsigned int
        await publisher.send_multipart([b'meta',
                                        np.array([state[FRAMENUMREG], 0],
                                                 dtype='<u4')])
        return np.sum(num_per_msg, dtype=np.intp)

    while True:
        msg = await responder.recv_multipart()
        print(msg)
        for m in msg:
            cmd, addr, value = np.frombuffer(m, dtype=np.int32)
            cmd = CMDS(cmd)
            if cmd == CMDS.REG_WRITE:
                state[addr] = value
                await responder.send(m)
                if addr == 0 and value == 1:
                    start_time = time.time()
                    num_ev = await sim_data()
                    delta_time = time.time() - start_time
                    print(f'generated {num_ev} events in {delta_time} s')
            elif cmd == CMDS.REG_READ:
                value = state[addr]
                reply = np.array([cmd.value, addr, value], dtype=np.uint32)
                await responder.send(reply)
            else:
                await responder.send(np.ones(3, dtype=np.uint32) * 0xdead)


loop.run_until_complete(recv_and_process())
