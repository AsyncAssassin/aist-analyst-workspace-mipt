"""Reject inherited writable resources before a D17 Compose command runs."""
import json
import os
from pathlib import Path
import re
import sys


def check(config):
    stack = os.environ.get('AIST_STACK_NAME', 'aist-d17')
    if not re.fullmatch(r'aist-d17(?:-[a-z0-9]+)*', stack) or config['name'] != stack:
        raise ValueError('D17 requires its own project name')
    volumes = config.get('volumes', {})
    for key, volume in volumes.items():
        if volume.get('external') or volume.get('name') != f'{stack}-{key}':
            raise ValueError(f'Unexpected volume: {key}')
    for key, network in config.get('networks', {}).items():
        if network.get('external') or network.get('name') != f'{stack}-{key}':
            raise ValueError(f'Unexpected network: {key}')
    runtime = Path('.runtime', stack, 'openclaw').resolve()
    services = {}
    for name, service in config['services'].items():
        if service.get('container_name', f'{stack}-{name}-1') != f'{stack}-{name}-1':
            raise ValueError(f'Unexpected container name: {name}')
        for mount in service.get('volumes', []):
            if mount['type'] == 'volume':
                if mount['source'] not in volumes:
                    raise ValueError(f'Unknown volume: {name}')
            elif mount['type'] == 'bind':
                source = Path(mount['source']).resolve()
                if not mount.get('read_only') and source != runtime:
                    raise ValueError(f'Writable bind outside D17 runtime: {name}')
            else:
                raise ValueError(f'Unsupported mount: {name}')
        for port in service.get('ports', []):
            if port.get('host_ip') != '127.0.0.1' or not 30710 <= int(port['published']) <= 30999:
                raise ValueError(f'Port outside the D17 loopback range: {name}')
        services[name] = {key: service[key] for key in ('image', 'ports', 'volumes', 'networks', 'network_mode') if key in service}
        if service.get('network_mode') == 'host':
            if name != 'worker':
                raise ValueError('Only the D17 worker may use the live host network')
            environment = service.get('environment', {})
            host_ports = {key: int(environment[key]) for key in
                          ('AIST_DB_PORT', 'AIST_GATEWAY_PORT', 'AIST_PROVIDER_PORT')}
            if any(not 30710 <= port <= 30999 for port in host_ports.values()):
                raise ValueError('Host network port outside the D17 range')
            if len(set(host_ports.values())) != len(host_ports):
                raise ValueError('Host network ports overlap')
            services[name]['host_ports'] = host_ports
        services[name]['container_name'] = f'{stack}-{name}-1'
    return dict(project=stack, services=services, volumes=volumes, networks=config.get('networks', {}))


if __name__ == '__main__':
    try:
        result = check(json.load(sys.stdin))
        if '--show' in sys.argv:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError) as error:
        sys.exit(str(error))
