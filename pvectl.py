#!/usr/bin/env python3
"""pvectl - gestiona VMs/CTs d'un clúster Proxmox VE sense saber en quin node són.

Nodes del clúster (es proven en ordre; si un no respon es passa al següent), per ordre de prioritat:
  -h nodes, --hosts nodes      llista separada per comes (es pot repetir)
  -f fitxer, --hosts-file fitxer   fitxer de text amb un node per línia (# = comentari)
  PVE_HOSTS                    variable d'entorn, separats per comes
  (-h i -f es poden combinar; van sempre abans del subcomandament; l'ajuda és --help)

Diverses VM: es poden indicar totes les VM/CT que es vulguin (nom o vmid). Per defecte
s'actua sobre una darrere l'altra; amb --parallel N, fins a N alhora. També es poden seleccionar
per patró (--match 'k8s-*', es pot repetir; exclou les plantilles) o per fitxer (--vms-file, un nom
o vmid per línia). Tot es pot combinar; van abans del subcomandament.

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
  pvectl.py --parallel 4 start web01 web02 web03 web04
  pvectl.py shutdown 105
  pvectl.py snapshot web01 pre-update --desc "abans d'actualitzar" [--vmstate]
  pvectl.py --parallel 3 snapshot web01 web02 web03 nocturn --keep 2   # el NOM és l'últim argument
  pvectl.py --match 'k8s-*' --parallel 4 shutdown
  pvectl.py --match 'web*' snapshot nocturn --keep 7
  pvectl.py --vms-file vms.txt status
  pvectl.py snapshots web01
  pvectl.py rollback web01 pre-update
  pvectl.py --yes delsnap web01 web02 pre-update
"""
import argparse
import fnmatch
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

POWER_ACTIONS = ("start", "stop", "shutdown", "reboot", "reset", "suspend", "resume")
# Accions que, sobre més d'una VM, exigeixen confirmació (o --yes)
DESTRUCTIVE = ("stop", "reset", "rollback", "delsnap")
# Subcomandaments amb la forma "VM [VM...] NOM": el nom és l'últim argument
NAMED = ("snapshot", "rollback", "delsnap")
DEFAULT_TOKEN_FILE = os.path.expanduser("~/.config/pvectl/token")
# Errors transitoris de bloqueig de la VM (una altra tasca hi treballa): l'operació no s'ha
# arribat a executar, així que és segur tornar-ho a provar.
LOCK_ERRORS = ("can't lock file", "is locked")


class PveError(Exception):
    """Error d'una operació contra el clúster (es reporta per VM, sense aturar les altres)."""


class ApiError(PveError):
    """Error HTTP retornat per l'API de Proxmox."""

    def __init__(self, code, host, body):
        super().__init__(f"Error {code} de {host}: {body}")
        self.body = body


class TaskFailed(PveError):
    """Una tasca de Proxmox ha acabat amb un exitstatus diferent d'OK."""

    def __init__(self, status):
        super().__init__(f"tasca fallida: {status}")


def is_lock_error(exc):
    msg = exc.body if isinstance(exc, ApiError) else str(exc)
    return any(s in msg for s in LOCK_ERRORS)


_print_lock = threading.Lock()


def say(msg, err=False):
    """Imprimeix una línia sencera sense que es barregi amb la d'un altre fil."""
    with _print_lock:
        print(msg, file=sys.stderr if err else sys.stdout, flush=True)


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
        raise PveError(f"Cap node accessible ({last})")

    def find_all(self, idents, patterns=()):
        """Resol noms/vmids i patrons en una sola consulta. Tot o res: si un falla, no es fa res.

        Primer van les VMs indicades explícitament (en ordre) i després les dels patrons (per vmid).
        Els patrons no inclouen plantilles.
        """
        vms = [v for v in self.call("GET", "/cluster/resources") if v.get("type") in ("qemu", "lxc")]
        found, errors = {}, []
        for ident in idents:
            hits = [v for v in vms if str(v["vmid"]) == ident or v.get("name") == ident]
            if not hits:
                errors.append(f"No trobo cap VM/CT amb vmid o nom '{ident}'")
            elif len(hits) > 1:
                errors.append(f"'{ident}' és ambigu: vmids {[v['vmid'] for v in hits]}. Feu servir el vmid.")
            else:
                found.setdefault(hits[0]["vmid"], hits[0])  # sense duplicats, mantenint l'ordre
        for pat in patterns:
            hits = [v for v in sorted(vms, key=lambda v: v["vmid"])
                    if not v.get("template") and fnmatch.fnmatchcase(v.get("name", ""), pat)]
            if not hits:
                errors.append(f"Cap VM/CT (que no sigui plantilla) coincideix amb el patró '{pat}'")
            for v in hits:
                found.setdefault(v["vmid"], v)
        if errors:
            sys.exit("\n".join(errors))
        return list(found.values())

    def wait(self, node, upid, timeout=600):
        end = time.time() + timeout
        while time.time() < end:
            st = self.call("GET", f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/status")
            if st["status"] == "stopped":
                if st.get("exitstatus") != "OK":
                    raise TaskFailed(st.get("exitstatus"))
                return
            time.sleep(2)
        raise PveError("temps d'espera esgotat (la tasca pot continuar al clúster)")

    def run_task(self, node, method, path, params=None, wait=True, retries=5, delay=10, label=""):
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
                say(f"{label}VM bloquejada per una altra tasca; reintent {attempt + 1}/{retries} "
                    f"d'aquí a {delay}s...", err=True)
                time.sleep(delay)


# Proxmox no admet dos snapshots amb el mateix nom a la mateixa VM. Amb --keep, el nom donat és
# un prefix i s'hi afegeix un sufix de data, que ordena cronològicament.
SNAP_SUFFIX_LEN = len("-AAAAMMDD-HHMMSS")
SNAP_NAME_MAX = 40  # llargada màxima del nom d'un snapshot a Proxmox


def prune_snapshots(pve, vm, base, prefix, keep, retries, delay, label=""):
    """Esborra els snapshots '<prefix>-AAAAMMDD-HHMMSS' més antics, deixant-ne `keep`."""
    pat = re.compile(re.escape(prefix) + r"-\d{8}-\d{6}$")
    group = sorted(s["name"] for s in pve.call("GET", f"{base}/snapshot") if pat.match(s["name"]))
    for old in group[:-keep]:
        pve.run_task(vm["node"], "DELETE", f"{base}/snapshot/{old}",
                     retries=retries, delay=delay, label=label)
        say(f"{label}Esborrat snapshot antic: {old}")


def read_vms_file(path):
    """Llegeix un fitxer amb una VM (nom o vmid) per línia; ignora buides i comentaris (#)."""
    try:
        with open(os.path.expanduser(path)) as f:
            return [l for l in (line.split("#", 1)[0].strip() for line in f) if l]
    except OSError as e:
        sys.exit(f"No puc llegir el fitxer de VMs ({path}): {e.strerror}")


def confirm_destructive(cmd, vms):
    """Demana confirmació (només en interactiu) abans d'una acció destructiva sobre diverses VM."""
    names = ", ".join(f"{v['vmid']} ({v.get('name')})" for v in vms)
    if not sys.stdin.isatty():
        sys.exit(f"'{cmd}' sobre {len(vms)} VMs requereix --yes (no hi ha terminal per confirmar-ho).")
    print(f"S'executarà '{cmd}' sobre {len(vms)} VMs: {names}")
    if input("Continuar? [s/N] ").strip().lower() not in ("s", "si", "sí", "y", "yes"):
        sys.exit("Cancel·lat.")


def main():
    try:
        run()
    except PveError as e:
        sys.exit(str(e))


def run():
    # add_help=False: el -h queda lliure per als nodes (l'ajuda és --help)
    ap = argparse.ArgumentParser(description="Gestió de VMs Proxmox", add_help=False)
    ap.add_argument("--help", action="help", help="mostra aquesta ajuda i surt")
    ap.add_argument("-h", "--hosts", action="append", metavar="NODES",
                    help="nodes del clúster separats per comes (es pot repetir)")
    ap.add_argument("-f", "--hosts-file", metavar="FITXER",
                    help="fitxer de text amb un node per línia (# = comentari)")
    ap.add_argument("--match", action="append", metavar="PATRÓ",
                    help="selecciona les VMs el nom de les quals coincideix amb el patró (p.ex. 'k8s-*'; "
                         "es pot repetir; exclou plantilles; citeu-lo perquè la shell no l'expandeixi)")
    ap.add_argument("--vms-file", metavar="FITXER",
                    help="fitxer amb una VM (nom o vmid) per línia (# = comentari)")
    ap.add_argument("--no-wait", action="store_true", help="no esperis que acabi la tasca")
    ap.add_argument("--parallel", type=int, default=1, metavar="N",
                    help="nombre de VMs a tractar alhora (defecte: 1, una darrere l'altra)")
    ap.add_argument("--yes", action="store_true",
                    help="no demanis confirmació per a accions destructives sobre diverses VMs")
    ap.add_argument("--retries", type=int, default=5, metavar="N",
                    help="reintents si la VM està bloquejada per una altra tasca (defecte: 5)")
    ap.add_argument("--retry-delay", type=int, default=10, metavar="SEGONS",
                    help="espera entre reintents (defecte: 10)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    subs = {}
    for c in POWER_ACTIONS + ("status", "snapshots"):
        subs[c] = sub.add_parser(c)
        subs[c].add_argument("vm", nargs="*", metavar="VM",
                             help="VMs (nom o vmid); opcional si s'usa --match o --vms-file")
    for c in NAMED:
        subs[c] = p = sub.add_parser(c)
        p.add_argument("args", nargs="*", metavar="VM... NOM",
                       help="VMs (nom o vmid) i, al final, el nom del snapshot; "
                            "amb --match o --vms-file només cal el nom")
    p = subs["snapshot"]
    p.add_argument("--desc", default="")
    p.add_argument("--vmstate", action="store_true", help="inclou la RAM (només qemu)")
    p.add_argument("--keep", type=int, metavar="N",
                   help="rotació: el nom és un prefix, s'hi afegeix la data (nom-AAAAMMDD-HHMMSS) "
                        "i es conserven només els N snapshots més recents d'aquest prefix")
    a = ap.parse_args()

    selectors = bool(a.match or a.vms_file)  # VMs triades per patró o fitxer, no a la línia d'ordres
    if a.cmd in NAMED:
        if len(a.args) < (1 if selectors else 2):
            subs[a.cmd].error("cal indicar el nom del snapshot i, si no s'usa --match ni --vms-file, "
                              "almenys una VM (VM... NOM)")
        a.vm, a.name = a.args[:-1], a.args[-1]
    elif a.cmd != "list" and not a.vm and not selectors:
        subs[a.cmd].error("cal indicar almenys una VM, o bé --match / --vms-file")

    if a.parallel < 1:
        sys.exit("--parallel ha de ser 1 o més.")
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
        vms = [v for v in pve.call("GET", "/cluster/resources") if v.get("type") in ("qemu", "lxc")]
        if a.match:  # a list, --match només filtra (mostra també les plantilles)
            vms = [v for v in vms if any(fnmatch.fnmatchcase(v.get("name", ""), p) for p in a.match)]
        for v in sorted(vms, key=lambda v: v["vmid"]):
            print(f"{v['vmid']:>6}  {v.get('name', '-'):<30} {v['type']:<5} {v.get('status', '?'):<9} {v['node']}")
        return

    vms = pve.find_all(a.vm + (read_vms_file(a.vms_file) if a.vms_file else []), a.match or ())
    many = len(vms) > 1
    if many and a.cmd in DESTRUCTIVE and not a.yes:
        confirm_destructive(a.cmd, vms)

    # Un sol sufix per a tota l'execució: totes les VMs reben el mateix nom de snapshot
    snapname = getattr(a, "name", None)
    if keep is not None:
        snapname = f"{a.name}-{time.strftime('%Y%m%d-%H%M%S')}"

    def work(vm):
        """Fa l'operació sobre una VM. Retorna True si ha anat bé; els errors es reporten aquí."""
        label = f"{vm['vmid']} ({vm.get('name')})"
        prefix = f"[{label}] " if many else ""
        base = f"/nodes/{vm['node']}/{vm['type']}/{vm['vmid']}"
        try:
            if a.cmd == "status":
                s = pve.call("GET", f"{base}/status/current")
                say(f"{vm['vmid']} {s.get('name')} @ {vm['node']}: {s['status']}")
                return True
            if a.cmd == "snapshots":
                # 'current' és una pseudo-entrada de Proxmox ("You are here!"), no un snapshot
                lines = [f"{s['name']:<30} {s.get('description', '')}"
                         for s in pve.call("GET", f"{base}/snapshot") if s["name"] != "current"]
                if many:
                    lines.insert(0, f"== {label} @ {vm['node']}")
                if lines:
                    say("\n".join(lines))
                return True

            params = None
            if a.cmd in POWER_ACTIONS:
                method, path = "POST", f"{base}/status/{a.cmd}"
            elif a.cmd == "snapshot":
                method, path = "POST", f"{base}/snapshot"
                params = {"snapname": snapname, "description": a.desc}
                if a.vmstate:
                    params["vmstate"] = 1
            elif a.cmd == "rollback":
                method, path = "POST", f"{base}/snapshot/{a.name}/rollback"
            elif a.cmd == "delsnap":
                method, path = "DELETE", f"{base}/snapshot/{a.name}"

            upid = pve.run_task(vm["node"], method, path, params, wait=not a.no_wait,
                                retries=a.retries, delay=a.retry_delay, label=prefix)
            if a.no_wait or not upid:
                say(f"Tasca enviada: {a.cmd} {label} a {vm['node']}: {upid}")
            else:
                what = f"{a.cmd} '{snapname}'" if keep is not None else a.cmd
                say(f"OK: {what} {label} a {vm['node']}")
                if keep is not None:
                    prune_snapshots(pve, vm, base, a.name, keep, a.retries, a.retry_delay, prefix)
            return True
        except PveError as e:
            say(f"ERROR: {a.cmd} {label}: {e}", err=True)
            return False

    if a.parallel == 1 or not many:
        results = [work(vm) for vm in vms]
    else:
        with ThreadPoolExecutor(max_workers=min(a.parallel, len(vms))) as ex:
            results = list(ex.map(work, vms))

    failed = results.count(False)
    if many and (failed or a.cmd not in ("status", "snapshots")):  # a les consultes només si hi ha errors
        say(f"Resum: {len(vms) - failed} correctes, {failed} amb error", err=bool(failed))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
