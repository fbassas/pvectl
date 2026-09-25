# pvectl

Eina de línia d'ordres per gestionar màquines virtuals (qemu) i contenidors (lxc) d'un
clúster **Proxmox VE** via l'API REST, **sense haver de saber en quin node és cada VM**.
Resol el node amb `GET /cluster/resources` i fa la crida al node correcte.

Operacions: arrencar, aturar (dur o net), reiniciar, reset, suspendre/reprendre,
crear/llistar/revertir/esborrar snapshots i consultar l'estat.

## Requisits

- Python 3.8 o superior (no cal cap paquet extern).
- Accés de xarxa al port **8006/tcp** dels nodes Proxmox.
- Certificats TLS vàlids als nodes (CA pública o una CA que la màquina ja tingui de confiança).
- Un usuari de servei i un API token a Proxmox (vegeu més avall).

## Instal·lació

```bash
cd ~/pvectl
python3 -m venv .venv            # ja creat
source .venv/bin/activate
pip install -r requirements.txt  # no instal·la res, però és inofensiu
```

## Configuració

### Nodes del clúster

Cal indicar-ne almenys un; qualsevol node del clúster serveix. Es proven en ordre i, si un no
respon, es passa al següent. Es poden donar de tres maneres:

```bash
# 1. Per argument, separats per comes (el flag -h es pot repetir)
./pvectl.py -h pve01.example.org,pve02.example.org,pve03.example.org list

# 2. Per fitxer de text, un node per línia
./pvectl.py -f nodes.txt list

# 3. Per variable d'entorn
export PVE_HOSTS=pve01.example.org,pve02.example.org,pve03.example.org
./pvectl.py list
```

Exemple de `nodes.txt` (les línies buides i les que comencen per `#` s'ignoren):

```
# Clúster de producció
pve01.example.org
pve02.example.org
pve03.example.org
```

Notes:

- `-h` i `-f` es poden combinar (primer els de `-h`, després els del fitxer). Si se'n dona algun,
  **ignoren** `PVE_HOSTS`. Els duplicats s'eliminen.
- Els flags van **abans** del subcomandament. Com que `-h` s'usa per als nodes, l'ajuda és
  `--help`.
- Cada node pot portar port: `pve01.example.org:8006` (per defecte 8006).
- El fitxer de nodes no conté secrets, però si conté noms interns pot ser millor no versionar-lo.

### Autenticació i TLS

| Variable       | Obligatòria | Descripció |
|----------------|-------------|------------|
| `PVE_TOKEN`    | sí\* | `usuari@realm!idtoken=secret` |
| `PVE_TOKEN_FILE` | sí\* | Ruta d'un fitxer que conté el token (alternativa a `PVE_TOKEN`). |
| `PVE_HOSTS`    | no | Nodes separats per comes; alternativa als flags `-h`/`-f`. |
| `PVE_CA_FILE`  | no | CA o certificat per verificar TLS, si no és de confiança pel sistema. |
| `PVE_INSECURE` | no | `1` desactiva la verificació TLS. **Només per proves.** |

\* Cal una de les dues. Ordre de prioritat: `PVE_TOKEN`, després `PVE_TOKEN_FILE`, i si cap
no està definida, el fitxer per defecte `~/.config/pvectl/token` (si existeix).

Exemple:

```bash
export PVE_HOSTS=pve01.example.org,pve02.example.org,pve03.example.org
export PVE_TOKEN='svc-vmctl@pve!automatitzacio=SECRET'
```

### Guardar el token en un fitxer (recomanat)

No escriviu el secret directament a la shell (queda a l'historial). Guardeu-lo en un fitxer
amb permisos `600`; l'script el llegeix tot sol:

```bash
mkdir -p ~/.config/pvectl
install -m 600 /dev/null ~/.config/pvectl/token
echo 'svc-vmctl@pve!automatitzacio=SECRET' > ~/.config/pvectl/token
```

El fitxer conté només el token (s'ignoren les línies buides i les que comencen per `#`).
Amb la ruta per defecte `~/.config/pvectl/token` només cal definir `PVE_HOSTS`. Per fer servir
una altra ruta:

```bash
export PVE_TOKEN_FILE=/etc/pvectl/token
```

Si el fitxer és accessible per altres usuaris (permisos més oberts que `600`), l'script
mostra un avís.

## Crear l'usuari i el token a Proxmox

Des de la shell de qualsevol node del clúster (la configuració és compartida):

```bash
pveum role add VMCtl -privs "VM.Audit VM.PowerMgmt VM.Snapshot VM.Snapshot.Rollback"
pveum user add svc-vmctl@pve --comment "Automatitzacio VMs"
pveum aclmod /vms -user svc-vmctl@pve -role VMCtl
pveum user token add svc-vmctl@pve automatitzacio --privsep 0
```

L'última ordre mostra el secret **una sola vegada**. Amb `--privsep 0` el token hereta els
permisos de l'usuari. Per restringir-lo, useu `--privsep 1` i doneu els permisos al token:

```bash
pveum aclmod /vms -token 'svc-vmctl@pve!automatitzacio' -role VMCtl
```

## Ús

```bash
./pvectl.py list                                   # totes les VM/CT i el seu node
./pvectl.py status web01                           # estat d'una VM (per nom o vmid)

./pvectl.py start web01                            # arrencar
./pvectl.py shutdown web01                         # aturada neta (ACPI / guest agent)
./pvectl.py stop web01                             # aturada dura (com tallar el corrent)
./pvectl.py reboot web01
./pvectl.py reset web01                            # reset dur (només qemu)
./pvectl.py suspend web01
./pvectl.py resume web01

./pvectl.py snapshot web01 pre-update --desc "abans d'actualitzar"
./pvectl.py snapshot web01 amb-ram --vmstate       # inclou la RAM (només qemu)
./pvectl.py snapshot web01 nocturn --keep 2        # rotació: en conserva els 2 més recents
./pvectl.py snapshots web01                        # llistar
./pvectl.py rollback web01 pre-update              # revertir
./pvectl.py delsnap web01 pre-update               # esborrar
```

La VM es pot indicar pel **nom** o pel **vmid**. Si un nom es repeteix, feu servir el vmid.

Per defecte l'script espera que la tasca acabi i falla si no ha anat bé. Amb `--no-wait`
retorna l'UPID immediatament (l'opció va **abans** del subcomandament):

```bash
./pvectl.py --no-wait start web01
```

### Rotació de snapshots (`--keep`)

Proxmox **no permet dos snapshots amb el mateix nom** a la mateixa VM (`snapshot name '...'
already used`). Per poder repetir el mateix "nom" i no acumular snapshots, `snapshot` accepta
`--keep N`: el nom passa a ser un **prefix**, s'hi afegeix la data i l'hora, i es conserven només
els N snapshots més recents d'aquest prefix.

```bash
./pvectl.py snapshot web01 nocturn --keep 2
# OK: snapshot 'nocturn-20260925-153000' ...
# Esborrat snapshot antic: nocturn-20260921-153000     (si ja n'hi havia 2 abans)
```

- Sense `--keep`, el nom és exactament el que doneu i, si ja existeix, falla (comportament de sempre).
- Primer es crea el nou i **després** s'esborren els antics, de manera que mai et quedes sense
  cap snapshot si la creació falla.
- Només es toquen els snapshots que segueixen el patró `<prefix>-AAAAMMDD-HHMMSS`. Els altres
  snapshots de la VM, encara que s'assemblin, no s'esborren mai.
- L'antiguitat es decideix pel sufix de data del nom, que ordena cronològicament.
- El prefix pot fer com a màxim 24 caràcters (Proxmox limita el nom sencer a 40).
- No es pot combinar amb `--no-wait`, perquè cal esperar el snapshot abans d'esborrar els antics.
- Esborrar el snapshot més antic el fusiona amb el següent; en discs qcow2 grans pot trigar una
  mica. És segur per a ZFS, Ceph i qcow2.
- Va bé per programar-ho amb cron: `0 2 * * * /ruta/pvectl.py snapshot web01 nocturn --keep 7`.

### Reintents si la VM està bloquejada

Proxmox bloqueja la configuració d'una VM mentre hi ha una altra tasca activa (per exemple, un
`snapshot` just després d'un `start`) i llavors l'operació falla amb errors com
`can't lock file ... got timeout` o `VM is locked`. Com que en aquest cas l'operació no s'ha
arribat a executar, l'script la **reintenta automàticament**: fins a 5 cops, esperant 10 s
entre intents (el missatge es mostra per stderr). Es pot ajustar, sempre **abans** del
subcomandament:

```bash
./pvectl.py --retries 10 --retry-delay 30 snapshot web01 pre-update   # més paciència
./pvectl.py --retries 0 start web01                                   # sense reintents
```

Només es reintenten els errors de bloqueig; qualsevol altre error (permisos, snapshot
inexistent...) falla immediatament. Amb `--no-wait` només es reintenta si el bloqueig es
detecta en el moment de llançar l'operació.

## Notes

- **Snapshots:** l'emmagatzematge ha de suportar-los (ZFS, Ceph RBD, LVM-thin, qcow2 sobre
  directori/NFS...). En LVM gruixut no funcionen.
- **HA:** si la VM és gestionada per HA, `start` i `shutdown` ajusten l'estat HA i la VM pot
  trigar una mica a reaccionar.
- **Bloqueig de la VM:** un bloqueig provocat per una tasca llarga (p. ex. una còpia de
  seguretat) pot superar els reintents per defecte; augmenteu `--retries` o `--retry-delay`.
- **Errors habituals:** `403` = falten permisos al rol o al token; `Cap node accessible` =
  problema de xarxa/firewall (8006) o de certificat; `ambigu` = nom repetit, useu el vmid.
- **Caducitat dels certificats:** si un node té el certificat caducat, l'script no hi connectarà
  amb verificació TLS (i passarà al node següent de `PVE_HOSTS`).
- Executar-lo des d'una VM del mateix clúster funciona igual, però compte amb aturar o
  reiniciar la pròpia VM on s'executa l'script.

## Llicència

MIT. Vegeu el fitxer [LICENSE](LICENSE).
