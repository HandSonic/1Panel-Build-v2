"""Exact supported installer resource layouts, independent of release version."""

BASE = frozenset({'install.sh', '1pctl'} | {f'lang/{lang}.sh' for lang in ('en', 'fa', 'pt-BR', 'ru', 'zh')})
ROOT_SERVICES = frozenset(f'1panel-{part}.service' for part in ('core', 'agent'))
INIT_SCRIPTS = frozenset(f'initscript/1panel-{part}.{kind}' for part in ('core', 'agent')
                         for kind in ('init', 'openrc', 'procd', 'service'))
MODERN = BASE | INIT_SCRIPTS
ROOT_SYSTEMD = BASE | ROOT_SERVICES
ARCHIVE_RESOURCES = MODERN | ROOT_SERVICES


def resource_layout(names):
    names = set(names)
    if names == MODERN:
        return 'initscript'
    if names == ROOT_SYSTEMD:
        return 'root-systemd'
    raise ValueError('Incomplete, mixed or unsupported installer resource contract')


def archive_resources(resources):
    """Select an exact source layout; validate any redundant systemd copies."""
    if MODERN <= set(resources):
        extras = set(resources) - MODERN
        if extras and (extras != ROOT_SERVICES or any(resources[name] != resources['initscript/' + name] for name in ROOT_SERVICES)):
            raise ValueError('Conflicting official root/initscript service copies')
        return {name: raw for name, raw in resources.items() if name in MODERN}
    if resource_layout(resources) == 'root-systemd':
        validate_root_interface(resources['install.sh'], resources)
    return resources


def validate_root_interface(installer, names):
    from service_layout import service_layout
    interface = service_layout(installer, names)
    if interface['layout'] != 'root-systemd' or set(interface['required']) != ROOT_SERVICES:
        raise ValueError('Root service resources differ from installer interface')


def service_copies(names):
    """Only the initscript layout needs root copies; legacy bytes stay in place."""
    if resource_layout(names) == 'root-systemd':
        return ()
    return tuple(('initscript/' + name, name) for name in sorted(ROOT_SERVICES))
