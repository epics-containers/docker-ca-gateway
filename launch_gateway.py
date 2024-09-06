#!/usr/bin/env python
import argparse

import logging
import queue
import threading
import time
import os
import subprocess


from collections import namedtuple
from typing import Generator, List, Set, Tuple
from scapy.all import *

try:
    from kubernetes import client, config, watch
    has_kubelib = True
except ImportError:
    has_kubelib = False


log = logging.getLogger(__name__)
level2num = {"debug": logging.DEBUG,
             "info": logging.INFO,
             "warn": logging.WARN,
             "error": logging.ERROR}


ServiceEvent = namedtuple("ServiceEvent", ["type", "ip", "port"])


class ServiceEventType(object):
    ADDED = 'ADDED'
    DELETED = 'DELETED'


def services_events_task(namespace: str, port: int,
                         eventq: 'queue.Queue[ServiceEvent]'):
    """Contains loop to get services' events and push them to a queue

    Args:
        namespace: used to select namespace that services belongs to
        port: specify the UDP port that the service should be listening
        eventq: queue used to push events
    """
    while True:
        try:
            if not has_kubelib:
                log.error("kubernetes library not found")
            services_events = kubelib_services_events
            for events in services_events(namespace, port):
                log.info("Got services events %s", repr(events))
                for event in events:
                    eventq.put(event)
        except Exception as e:
            log.error("Problems: %s\nRetrying ...", e)
            time.sleep(5)


def kubelib_services_events(namespace: str,
                            port: int) -> Generator[List[ServiceEvent],
                                                    None,
                                                    None]:
    """Generator for services' events using kubernetes library

    Args:
        namespace: used to select namespace that services belongs to
        port: specify the UDP port that the service should be listening
    """
    config.load_kube_config()
    api_watch = watch.Watch()
    v1 = client.CoreV1Api()
    while True:
        for event in api_watch.stream(v1.list_namespaced_service,
                                      namespace=namespace):
            log.debug("Got kubernetes event: %s", event)
            result = []
            event_type = event.get('type')
            for ingress in event['object'].status.load_balancer.ingress:
                ip = ingress.ip
                for ports in event['object'].spec.ports:
                    srv_port = ports.port
                    proto = ports.protocol
                    if srv_port == port and proto == 'UDP':
                        result.append(ServiceEvent(event_type, ip, port))
            if result:
                yield result



def handle_events(eventq: 'queue.Queue[ServiceEvent]',
                  search_endpoints: Set[Tuple[str, int]]):
    """Updates search_endpoints acording to events received via an event queue

    Args:
        eventq: queue to receive services events
        search_endpoints: set that will be updated acording to events
    """
    while True:
        try:
            event = eventq.get(False)
        except queue.Empty:
            return
        if event.type == ServiceEventType.ADDED:
            search_endpoints.add((event.ip, event.port))
        elif event.type == ServiceEventType.DELETED:
            search_endpoints.discard((event.ip, event.port))
        else:
            log.error("Incorrect service event type: %s", event.type)

def get_ioc_ips(v1: client.CoreV1Api, namespace: str):
    """Get the list cluster IPs of IOCs running in a namespace

    Args:
        v1: kubernetes client
        namespace: namespace to get the IOCs from
    """
    ips = set()
    ret = v1.list_namespaced_pod(namespace)
    for pod in ret.items:
        if "is_ioc" in pod.metadata.labels:
            ips.add(pod.status.pod_ip)

    return ips



def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5064)
    parser.add_argument('--namespace', type=str, required=True)
    parser.add_argument('--loglevel', type=str, default="info")
    return parser.parse_args()


def main():
    args = parse_args()
    search_endpoints = set()
    logging.basicConfig(level=level2num.get(args.loglevel.lower(), "info"))

    # configure K8S and make a Core API client
    config.load_incluster_config()
    v1 = client.CoreV1Api()

    ips = get_ioc_ips(v1, args.namespace)
    ipstr = ",".join(ips)

    command = f"/epics/ca-gateway/bin/linux-x86_64/gateway -sport {args.port} -cip {ipstr} -pvlist /config/pvlist -access /config/access -log /dev/stdout -debug 1"

    print(f"Running command: {command}")
    subprocess.run(["bash", "-c", command], check=True)
    # eventq = queue.Queue()
    # threading.Thread(None, services_events_task, "services_events",
    #                  args=(args.namespace, args.port, eventq)).start()

    # while True:
    #     handle_events(eventq, search_endpoints)


if __name__ == "__main__":
    main()