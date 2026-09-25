#!/usr/bin/env python3
"""pvectl - gestiona VMs/CTs d'un clúster Proxmox VE sense saber en quin node són.

Nodes del clúster (es proven en ordre; si un no respon es passa al següent), per ordre de prioritat:
  -h nodes, --hosts nodes      llista separada per comes (es pot repetir)
  -f fitxer, --hosts-file fitxer   fitxer de text amb un node per línia (# = comentari)
  PVE_HOSTS                    variable d'entorn, separats per comes
  (-h i -f es poden combinar; van sempre abans del subcomandament; l'ajuda és --help)

Variables d'entorn:
  PVE_HOSTS    nodes separats per comes, p.ex. pve01.example.org,pve02.example.org
  PVE_TOKEN    usuari@realm!idtoken=secret
  PVE_TOKEN_FILE  (alternativa a PVE_TOKEN) fitxer que conté el token; si no es defineix
               cap de les dues, es prova ~/.config/pvectl/token. Hauria de tenir permisos 600.
  PVE_CA_FILE  (opcional) CA o certificat del clúster per verificar TLS
  PVE_INSECURE (opcional) =1 per no verificar TLS (només proves)

Exemples:
  pvectl.py -h pve01.example.org,pve02.example.org list
  pvectl.py -f nodes.txt list
  pvectl.py list
  pvectl.py start web01
  pvectl.py shutdown 105
  pvectl.py snapshot web01 pre-update --desc "abans d'actualitzar" [--vmstate]
  pvectl.py snapshot web01 nocturn --keep 2   # crea nocturn-AAAAMMDD-HHMMSS i en conserva només 2
  pvectl.py snapshots web01
  pvectl.py rollback web01 pre-update
  pvectl.py delsnap web01 pre-update
"""
import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POWER_ACTIONS = ("start", "stop", "shutdown", "reboot", "reset", "suspend", "resume")
DEFAULT_TOKEN_FILE = os.path.expanduser("~/.config/pvectl/token")
# Errors transitoris de bloqueig de la VM (una altra tasca hi treballa): l'operació no s'ha
# arribat a executar, així que és segur tornar-ho a provar.
LOCK_ERRORS = ("can't lock file", "is locked")


class ApiError(Exception):
    """Error HTTP retornat per l'API de Proxmox."""

    def __init__(self, code, host, body):
        super().__init__(f"Error {code} de {host}: {body}")
        self.body = body


class TaskFailed(Exception):
    """Una tasca de Proxmox ha acabat amb un exitstatus diferent d'OK."""


def is_lock_error(exc):
    msg = exc.body if isinstance(exc, ApiError) else str(exc)
    return any(s in msg for s in LOCK_ERRORS)


def load_token():
    """PVE_TOKEN té prioritat; si no, es llegeix PVE_TOKEN_FILE o el fitxer per defecte."""
    token = os.environ.get("PVE_TOKEN", "").strip()
    if token:
        return token
    explicit = os.environ.get("PVE_TOKEN_FILE")
    path = os.path.expanduser(explicit) if explicit else DEFAULT_TOKEN_FILE
    if not explicit and not os.path.exists(path):
        return ""
    try:
        if os.stat(path).st_mode & 0o077:
            print(f"Avís: {path} és accessible per altres usuaris; feu 'chmod 600 {path}'.",
                  file=sys.stderr)
        with open(path) as f:
            # Ignora línies buides i comentaris; agafa la primera línia útil
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except OSError as e:
        sys.exit(f"No puc llegir el fitxer del token ({path}): {e.strerror}")
    sys.exit(f"El fitxer del token ({path}) és buit.")


def resolve_hosts(cli_hosts, hosts_file):
    """Nodes de -h i -f (en aquest ordre); si no n'hi ha cap, els de PVE_HOSTS. Sense duplicats."""
    hosts = [h for arg in cli_hosts or [] for h in arg.split(",")]
    if hosts_file:
        try:
            with open(os.path.expanduser(hosts_file)) as f:
                for line in f:
                    line = line.split("#", 1)[0]
                    hosts.extend(line.split(","))  # també tolera comes dins la línia
        except OSError as e:
            sys.exit(f"No puc llegir el fitxer de nodes ({hosts_file}): {e.strerror}")
    if not hosts:
        hosts = (os.environ.get("PVE_HOSTS") or os.environ.get("PVE_HOST", "")).split(",")
    return list(dict.fromkeys(h.strip() for h in hosts if h.strip()))


class Pve:
    def __init__(self, hosts):
        self.hosts = hosts
        self.token = load_token()
        if not self.hosts:
            sys.exit("Cal indicar els nodes amb -h, -f o PVE_HOSTS (vegeu la capçalera de l'script).")
        if not self.token:
            sys.exit("Cal definir PVE_TOKEN, PVE_TOKEN_FILE o crear " + DEFAULT_TOKEN_FILE)
        if os.environ.get("PVE_INSECURE") == "1":
            self.ctx = ssl._create_unverified_context()
        else:
            self.ctx = ssl.create_default_context(cafile=os.environ.get("PVE_CA_FILE"))

    def call(self, method, path, params=None):
        data = urllib.parse.urlencode(params).encode() if params else None
        last = None
        for host in self.hosts:
            host_port = host if ":" in host else host + ":8006"
            req = urllib.request.Request(
                f"https://{host_port}/api2/json{path}", data=data, method=method,
                headers={"Authorization": f"PVEAPIToken={self.token}"})
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=30) as r:
                    return json.load(r)["data"]
            except urllib.error.HTTPError as e:
                # Error de l'API (permisos, VM inexistent...): no té sentit provar un altre node
                raise ApiError(e.code, host, e.read().decode(errors="replace"))
            except (urllib.error.URLError, OSError) as e:
                last = e  # node caigut o inaccessible: provem el següent
        sys.exit(f"Cap node accessible ({last})")

    def find(self, ident):
        vms = self.call("GET", "/cluster/resources")
        vms = [v for v in vms if v.get("type") in ("qemu", "lxc")]
        hits = [v for v in vms if str(v["vmid"]) == ident or v.get("name") == ident]
        if not hits:
            sys.exit(f"No trobo cap VM/CT amb vmid o nom '{ident}'")
        if len(hits) > 1:
            sys.exit(f"'{ident}' és ambigu: vmids {[v['vmid'] for v in hits]}. Feu servir el vmid.")
        return hits[0]

    def wait(self, node, upid, timeout=600):
        end = time.time() + timeout
        while time.time() < end:
            st = self.call("GET", f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/status")
            if st["status"] == "stopped":
                if st.get("exitstatus") != "OK":
                    raise TaskFailed(st.get("exitstatus"))
                return
            time.sleep(2)
        sys.exit("Temps d'espera esgotat (la tasca pot continuar al clúster)")

    def run_task(self, node, method, path, params=None, wait=True, retries=5, delay=10):
        """Llança una operació i (si wait) n'espera el resultat.

        Si la VM està bloquejada per una altra tasca, reintenta fins a `retries` cops
        esperant `delay` segons entre intents. Qualsevol altre error és definitiu.
        """
        for attempt in range(retries + 1):
            try:
                upid = self.call(method, path, params)
                if wait and upid:
                    self.wait(node, upid)
                return upid
            except (ApiError, TaskFailed) as e:
                if not is_lock_error(e) or attempt == retries:
                    raise
                print(f"VM bloquejada per una altra tasca; reintent {attempt + 1}/{retries} "
                      f"d'aquí a {delay}s...", file=sys.stderr)
                time.sleep(delay)


# Proxmox no admet dos snapshots amb el mateix nom a la mateixa VM. Amb --keep, el nom donat és
# un prefix i s'hi afegeix un sufix de data, que ordena cronològicament.
SNAP_SUFFIX_LEN = len("-AAAAMMDD-HHMMSS")
SNAP_NAME_MAX = 40  # llargada màxima del nom d'un snapshot a Proxmox


def prune_snapshots(pve, vm, base, prefix, keep, retries, delay):
    """Esborra els snapshots '<prefix>-AAAAMMDD-HHMMSS' més antics, deixant-ne `keep`."""
    pat = re.compile(re.escape(prefix) + r"-\d{8}-\d{6}$")
    group = sorted(s["name"] for s in pve.call("GET", f"{base}/snapshot") if pat.match(s["name"]))
    for old in group[:-keep]:
        pve.run_task(vm["node"], "DELETE", f"{base}/snapshot/{old}", retries=retries, delay=delay)
        print(f"Esborrat snapshot antic: {old}")


def main():
    try:
        run()
    except ApiError as e:
        sys.exit(str(e))
    except TaskFailed as e:
        sys.exit(f"Tasca fallida: {e}")


def run():
    # add_help=False: el -h queda lliure per als nodes (l'ajuda és --help)
    ap = argparse.ArgumentParser(description="Gestió de VMs Proxmox", add_help=False)
    ap.add_argument("--help", action="help", help="mostra aquesta ajuda i surt")
    ap.add_argument("-h", "--hosts", action="append", metavar="NODES",
                    help="nodes del clúster separats per comes (es pot repetir)")
    ap.add_argument("-f", "--hosts-file", metavar="FITXER",
                    help="fitxer de text amb un node per línia (# = comentari)")
    ap.add_argument("--no-wait", action="store_true", help="no esperis que acabi la tasca")
    ap.add_argument("--retries", type=int, default=5, metavar="N",
                    help="reintents si la VM està bloquejada per una altra tasca (defecte: 5)")
    ap.add_argument("--retry-delay", type=int, default=10, metavar="SEGONS",
                    help="espera entre reintents (defecte: 10)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for c in POWER_ACTIONS + ("status", "snapshots"):
        sub.add_parser(c).add_argument("vm")
    p = sub.add_parser("snapshot")
    p.add_argument("vm"); p.add_argument("name")
    p.add_argument("--desc", default="")
    p.add_argument("--vmstate", action="store_true", help="inclou la RAM (només qemu)")
    p.add_argument("--keep", type=int, metavar="N",
                   help="rotació: el nom és un prefix, s'hi afegeix la data (nom-AAAAMMDD-HHMMSS) "
                        "i es conserven només els N snapshots més recents d'aquest prefix")
    for c in ("rollback", "delsnap"):
        p = sub.add_parser(c)
        p.add_argument("vm"); p.add_argument("name")
    a = ap.parse_args()

    keep = getattr(a, "keep", None)
    if keep is not None:
        if keep < 1:
            sys.exit("--keep ha de ser 1 o més.")
        if a.no_wait:
            sys.exit("--keep no es pot combinar amb --no-wait: cal esperar el snapshot per poder esborrar els antics.")
        if len(a.name) + SNAP_SUFFIX_LEN > SNAP_NAME_MAX:
            sys.exit(f"Amb --keep el nom s'allarga {SNAP_SUFFIX_LEN} caràcters; el prefix pot fer "
                     f"com a màxim {SNAP_NAME_MAX - SNAP_SUFFIX_LEN}.")

    pve = Pve(resolve_hosts(a.hosts, a.hosts_file))
    if a.cmd == "list":
        vms = pve.call("GET", "/cluster/resources")
        for v in sorted((v for v in vms if v.get("type") in ("qemu", "lxc")), key=lambda v: v["vmid"]):
            print(f"{v['vmid']:>6}  {v.get('name', '-'):<30} {v['type']:<5} {v.get('status', '?'):<9} {v['node']}")
        return

    vm = pve.find(a.vm)
    base = f"/nodes/{vm['node']}/{vm['type']}/{vm['vmid']}"

    if a.cmd == "status":
        s = pve.call("GET", f"{base}/status/current")
        print(f"{vm['vmid']} {s.get('name')} @ {vm['node']}: {s['status']}")
        return
    if a.cmd == "snapshots":
        for s in pve.call("GET", f"{base}/snapshot"):
            if s["name"] == "current":  # pseudo-entrada de Proxmox ("You are here!"), no és un snapshot
                continue
            print(f"{s['name']:<30} {s.get('description', '')}")
        return

    params = None
    if a.cmd in POWER_ACTIONS:
        method, path = "POST", f"{base}/status/{a.cmd}"
    elif a.cmd == "snapshot":
        method, path = "POST", f"{base}/snapshot"
        snapname = a.name if keep is None else f"{a.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        params = {"snapname": snapname, "description": a.desc}
        if a.vmstate:
            params["vmstate"] = 1
    elif a.cmd == "rollback":
        method, path = "POST", f"{base}/snapshot/{a.name}/rollback"
    elif a.cmd == "delsnap":
        method, path = "DELETE", f"{base}/snapshot/{a.name}"

    upid = pve.run_task(vm["node"], method, path, params, wait=not a.no_wait,
                        retries=a.retries, delay=a.retry_delay)
    if a.no_wait or not upid:
        print(f"Tasca enviada: {upid}")
    else:
        what = f"{a.cmd} '{snapname}'" if keep is not None else a.cmd
        print(f"OK: {what} {vm['vmid']} ({vm.get('name')}) a {vm['node']}")
        if keep is not None:
            prune_snapshots(pve, vm, base, a.name, keep, a.retries, a.retry_delay)


if __name__ == "__main__":
    main()
