#!/usr/bin/python2.7
# encoding: utf8
"""
Monitoring tool for LightwaveRF Heating, based on the API published at
https://api.lightwaverf.com/introduction_basic_comms.html

This tool requires a LightwaveRF Link to bridge the UDP/IP network on which
this tool runs, and the LightwaveRF devices (which do not use WiFI).
"""

# pylint: disable=invalid-name,trailing-whitespace,missing-docstring

import sys
import logging
import logging.handlers
import prometheus_client

logging.basicConfig(
    format="%(asctime)-15s %(levelname)-7s %(message)s ",
    level=logging.NOTSET)

sLog = logging.getLogger()

sLog.info("Logging started...")

fh = logging.FileHandler('monitor.log')
fh.setLevel(logging.NOTSET)
sLog.addHandler(fh)

COMMAND = "!R{RoomNo}D{DeviceNo}F{FunctionType}P{Parameter}|{Line1}|{Line2}"

# =============================================================================
class ProtectedAttribute(object):
    """
    Descriptor which protects all data access (get, set) with its host
    instance's `sLock` attribute.
    """
    def __get__(self, sHostInstance, clsType=None):
        del clsType
        with sHostInstance.sLock:
            return self.mValue
    def __set__(self, sHostInstance, mNewValue):
        with sHostInstance.sLock:
            self.mValue = mNewValue

# =============================================================================
class LightwaveLink(object):
    """
    :IVariables:
        sSock : socket.socket
            UDP/IP socket used for both transmitting commands to the Lightwave
            Link and receiving responses. Note that responses are received on a
            different port number than commands are sent on.
        rAddress : str
            IPv4 dotted decimal representation of the Lightwave Link's IP
            address if known. Initially set to the broadcast address,
            `255.255.255.255`.
        siTransactionNumber : int generator
            Generator object which yields integers with monotonically
            increasing values. These are used to give every command transmitted
            a unique transaction number. Experiment has shown the Lightwave
            Link will not respond, at all, if the command lacks a transaction
            number.
        sResponses : queue.Queue
            Sequence of messages received from the Lightwave Link. Note that
            because the Lightwave Link broadcasts all its responses, the
            responses may not be related to any commands sent by us.
        sLock : threading.RLock
            Mutual exclusion (mutex) lock that guards access to attributes.
        fLastCommandTime : float
            Unixtime when last command was issued. Used to implement rate
            limiting.
        iLastTransactionNumber : int
            Most recently transmitted transaction number sent by
            `send_command`.
        rLastCommand : str
            Most recently transmitted command string sent by `send_command`.
            Used to retransmit in response to "Transmit fail" errors from the
            Lightwave Link.
    """
    LIGHTWAVE_LINK_COMMAND_PORT = 9760    # Send to this address...
    LIGHTWAVE_LINK_RESPONSE_PORT = 9761   # ... and get response on this one
    MIN_SECONDS_BETWEEN_COMMANDS = 3.0
    COMMAND_TIMEOUT_SECONDS = 5

    fLastCommandTime = ProtectedAttribute()
    iLastTransactionNumber = ProtectedAttribute()
    rLastCommand = ProtectedAttribute()

    sPResponseDelay = prometheus_client.Gauge(
        "lwl_response_delay_seconds",
        "Time between command being issued and response being recieved",
        ["fn",],
        )
    sPResponseCounter = prometheus_client.Counter(
        "lwl_responses",
        "Number of distinct JSON message received",
        ["fn",],
        )

    def __init__(self, port):
        import threading
        self.sSock = self.create_socket(port)
        self.rAddress = "255.255.255.255"
        self.sResponses = self.create_listener(self.sSock)
        self.sLock = threading.RLock()
        self.fLastCommandTime = 0.0
        self.iLastTransactionNumber = 0
        self.rLastCommand = ""
        self.sThead = None

    def create_socket(self, port):
        """Create a listening socket to receive UDP messages going to the Lightwave
        Link"""
        import socket
        sSock = socket.socket(
            socket.AF_INET, 
            socket.SOCK_DGRAM)
        tLocalAddress = (
            "0.0.0.0",
            port)
        sSock.bind(tLocalAddress)
        sSock.setsockopt(
            socket.SOL_SOCKET, 
            socket.SO_BROADCAST,
            1)
        return sSock

    def get_response(self):
        import time
        try:
            fTimeout = (
                        self.COMMAND_TIMEOUT_SECONDS
                        + time.time() 
                        - self.fLastCommandTime 
                        )
            dResponse = self.sResponses.get(True, fTimeout)
            fDelay = time.time() - self.fLastCommandTime
            rFn = dResponse.get("fn", "")
            self.sPResponseDelay.labels(rFn).set(fDelay)
            self.sPResponseCounter.labels(rFn).inc()
            return dResponse
        except self.sResponses.Empty:
            return {}

    def create_listener(self, sSock):
        import threading
        import Queue as queue
        sQueue = queue.Queue()
        sQueue.Empty = queue.Empty
        def run():
            """Responses are send twice, once unicast and another broadcast.
            This makes duplicate messages very common."""
            import json
            import collections
            # nonlocal sSock
            # nonlocal sQueue
            iTransactionNumber = 0
            lPreviousMessages = collections.deque(maxlen=10)
            while True:
                rMessage = sSock.recv(1024)
                if rMessage in lPreviousMessages:
                    sLog.log(1, "Ignoring duplicate JSON message")
                    continue
                lPreviousMessages.appendleft(rMessage)
                sLog.log(2, "RAW response: %s", rMessage)
                if rMessage.startswith("*!{"):
                    rJSON = rMessage[len("*!"):]
                    dMessage = json.loads(rJSON)
                    iResponseTrans = int(dMessage.get("trans", 0))
                    if iResponseTrans > iTransactionNumber:
                        iTransactionNumber = iResponseTrans
                        sQueue.put(dMessage)
                    else:
                        sLog.log(
                            1, 
                            "Discarding duplicate trans: %s", 
                            iResponseTrans)
                elif rMessage.strip().endswith(",OK"):
                    sLog.log(1, "Ignoring acknowledgement")
                else:
                    sLog.warning(
                        "Discarding non-JSON response: %s",
                        rMessage)
        def runner():
            # nonlocal run
            # nonlocal sLog
            while True:
                # pylint: disable=bare-except
                try:
                    run()
                except:
                    sLog.error(
                        "Exception from Lightwave Listener thread",
                        exc_info=True)

        self.sThread = threading.Thread(
            target=runner,
            name="LightwaveLink Listener"
            )
        self.sThread.daemon = True   # Do not prevent process exit()
        self.sThread.start()
        return sQueue

    def monitor(self, desc):
        sLog.info("Monitoring %s...", desc)
        try:
            while True:
                dResponse = self.get_response()
                sLog.debug("%s %s: %s", dResponse.get('fn'), desc, dResponse)
        except KeyboardInterrupt:
            sLog.warn("Aborting due to SIGINT")
            return False

def main():
    prometheus_client.start_http_server(9191)

    commands = LightwaveLink(9760)
    #responses = LightwaveLink(9761)

    import threading
    threading.Thread(target = commands.monitor, args = ("command ", )).start()
    #threading.Thread(target = responses.monitor, args = ("response", )).start()
    
    """
    sLog.info("Monitoring commands...")
    try:
        while True:
            dCommand  =  commands.get_response()
            dResponse = responses.get_response()
            
            #sLog.debug("%s response: %s", dResponse.get('fn'), dResponse)
    except KeyboardInterrupt:
        sLog.warn("Aborting registration process due to SIGINT")
        return False
    """

if __name__ == "__main__":
    main()
