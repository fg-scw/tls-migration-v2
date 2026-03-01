# VMware to Scaleway Migration Tool — Design Document

## Nom de code : `vmware2scw`

**Version** : 0.1.0-draft
**Auteur** : Architecture & Design
**Date** : 2026-02-18

---

## 1. Executive Summary

`vmware2scw` est un outil CLI (avec possibilité d'interface web future) qui automatise la migration de machines virtuelles depuis un environnement VMware (vSphere/vCenter) vers des Instances Scaleway. L'outil supporte exclusivement le format `.qcow2` comme format cible, en accord avec les exigences de Scaleway.

### 1.1 Inspirations & Références

- **[vjailbreak](https://github.com/platform9/vjailbreak)** — Outil open-source de Platform9 pour migration VMware → OpenStack. Architecture Go, utilise `govmomi` pour l'interaction vCenter, `virt-v2v-in-place` pour la conversion, `nbdkit` + VDDK pour la copie de disques via NBD.
- **[Doc Scaleway : Migration VMware](https://www.scaleway.com/en/docs/instances/reference-content/migrating-vms-vmware-scaleway/)** — Procédure officielle Scaleway.

---

## 2. Analyse des étapes de migration

### 2.1 Pipeline complet (corrigé et enrichi)

Voici la liste complète des étapes, **incluant les éléments manquants dans ta spécification initiale** :

| # | Étape | Confiance | Notes |
|---|-------|-----------|-------|
| 1 | **Connexion vSphere/vCenter** | 95 | Via `govmomi` (Go) ou `pyvmomi` (Python). API bien documentée |
| 2 | **Inventaire des VMs** | 95 | Listing complet : CPU, RAM, disques, NICs, OS, power state, snapshots |
| 3 | **Sélection des VMs** (batch/individuel) | 90 | UI interactive ou fichier de configuration YAML/JSON |
| 4 | **Mapping des ressources** | 85 | OS → type d'instance SCW, NICs → VPC/VLAN, disques → volumes |
| 5 | ⚠️ **Pré-validation de compatibilité** | 80 | Vérifier : taille disque ≤ limites SCW, OS supporté, UEFI/BIOS, etc. |
| 6 | ⚠️ **Snapshot de la VM source** | 90 | Requis avant export pour cohérence des données |
| 7 | **Export/copie des disques VMDK** | 80 | Via VDDK/NBD (in-fly) ou OVF export → fichier local/S3 transitoire |
| 8 | **Nettoyage VMware Tools** | 85 | `virt-v2v-in-place` ou `guestfish` pour supprimer/remplacer les drivers |
| 9 | ⚠️ **Injection des drivers VirtIO** | 85 | Critique pour le boot KVM. Windows: virtio-win ISO. Linux: généralement inclus dans le kernel |
| 10 | **Conversion VMDK → qcow2** | 92 | `qemu-img convert -f vmdk -O qcow2` — outil mature et fiable |
| 11 | ⚠️ **Adaptation bootloader** | 75 | ⚠️ SPÉCULATIF — Certaines VMs nécessitent un fix GRUB/fstab pour booter sur KVM |
| 12 | **Upload qcow2 → S3 Scaleway** | 90 | Multipart upload via SDK S3. Bucket transitoire ou définitif |
| 13 | ⚠️ **Import image dans Scaleway** | 82 | Création snapshot depuis S3, puis image, puis instance |
| 14 | ⚠️ **Configuration réseau post-migration** | 70 | ⚠️ SPÉCULATIF — Adaptation des configs réseau (netplan, ifcfg, etc.) |
| 15 | ⚠️ **Validation post-migration** | 75 | ⚠️ SPÉCULATIF — Health checks, vérification boot, connectivité |
| 16 | ⚠️ **Nettoyage des ressources transitoires** | 90 | Suppression snapshots VMware, fichiers temporaires, buckets transitoires |

> **⚠️ Éléments manquants dans ta spécification initiale** (marqués ⚠️) :
> - **Étape 5** : Pré-validation de compatibilité — Sans cette étape, on risque de convertir une VM qui ne peut pas tourner côté Scaleway
> - **Étape 6** : Snapshot source — Indispensable pour garantir la cohérence des données pendant l'export
> - **Étape 9** : Injection VirtIO — Sans les drivers VirtIO, la VM ne bootera pas sur KVM
> - **Étape 11** : Adaptation bootloader — Certains OS (surtout anciens) nécessitent une adaptation GRUB/fstab
> - **Étape 14** : Configuration réseau — Les interfaces réseau changent (vmxnet3 → virtio-net), les noms d'interface aussi
> - **Étape 15** : Validation post-migration — Essentiel pour confirmer le succès
> - **Étape 16** : Nettoyage — Snapshots VMware, fichiers temporaires

---

## 3. Architecture technique

### 3.1 Choix du langage : Python

**Justification** (Confiance: 88) :

| Critère | Python | Go |
|---------|--------|----|
| SDK VMware | `pyvmomi` (mature) | `govmomi` (mature) |
| SDK Scaleway | `scaleway-sdk-python` | Pas de SDK officiel Go |
| Conversion disque | `qemu-img` (appel système) | idem |
| virt-v2v | Appel système | idem |
| S3 upload | `boto3` (excellent) | AWS SDK Go |
| Rapidité de dev | ✅ Plus rapide | Plus performant runtime |
| Audience cible | ✅ Admins infra familiers | Moins courant en ops |

**Décision** : Python 3.11+ avec architecture modulaire. L'outil est principalement un orchestrateur d'appels système (`qemu-img`, `virt-v2v`, `guestfish`) et d'appels API.

### 3.2 Dépendances système requises

```
qemu-utils          # qemu-img pour conversion VMDK → qcow2
libguestfs-tools    # virt-v2v, virt-customize, guestfish, guestmount
nbdkit              # Serveur NBD (optionnel, pour copie in-fly via VDDK)
virtio-win          # Drivers Windows pour KVM (ISO)
```

### 3.3 Dépendances Python

```
pyvmomi >= 8.0      # VMware vSphere API
boto3               # S3 compatible (Scaleway Object Storage)
rich                # UI console (progress bars, tableaux)
click               # CLI framework
pyyaml              # Configuration
requests            # Scaleway API calls
jinja2              # Templates de configuration
pydantic >= 2.0     # Validation des données
```

### 3.4 Structure du projet

```
vmware2scw/
├── DESIGN.md                    # Ce document
├── README.md                    # Documentation utilisateur
├── pyproject.toml               # Build & dépendances
├── Dockerfile                   # Container avec toutes les dépendances système
├── docker-compose.yml           # Pour déploiement simple
│
├── src/
│   └── vmware2scw/
│       ├── __init__.py
│       ├── cli.py               # Point d'entrée CLI (Click)
│       ├── config.py            # Modèles de configuration (Pydantic)
│       │
│       ├── vmware/              # Module VMware
│       │   ├── __init__.py
│       │   ├── client.py        # Connexion vSphere/vCenter
│       │   ├── inventory.py     # Listing & inventaire VMs
│       │   ├── export.py        # Export VMDK (OVF ou NBD)
│       │   └── snapshot.py      # Gestion des snapshots
│       │
│       ├── converter/           # Module de conversion
│       │   ├── __init__.py
│       │   ├── vmware_tools.py  # Nettoyage VMware tools
│       │   ├── virtio.py        # Injection drivers VirtIO
│       │   ├── disk.py          # Conversion VMDK → qcow2
│       │   ├── bootloader.py    # Adaptation bootloader (GRUB, fstab)
│       │   └── network.py       # Adaptation config réseau
│       │
│       ├── scaleway/            # Module Scaleway
│       │   ├── __init__.py
│       │   ├── s3.py            # Upload S3 (Scaleway Object Storage)
│       │   ├── instance.py      # API Instance (import image, create instance)
│       │   ├── mapping.py       # Mapping ressources VMware → SCW
│       │   └── types.py         # Catalogue des types d'instances SCW
│       │
│       ├── pipeline/            # Orchestration
│       │   ├── __init__.py
│       │   ├── migration.py     # Pipeline principal
│       │   ├── validator.py     # Pré-validation compatibilité
│       │   ├── batch.py         # Gestion batch de migrations
│       │   └── state.py         # Gestion état / reprise après erreur
│       │
│       └── utils/               # Utilitaires
│           ├── __init__.py
│           ├── logging.py       # Logging structuré
│           ├── progress.py      # Barres de progression (rich)
│           └── subprocess.py    # Wrapper appels système
│
├── configs/
│   ├── mapping_templates/       # Templates de mapping VMware → SCW
│   │   ├── os_mapping.yaml      # Mapping OS → type instance
│   │   └── network_mapping.yaml # Mapping réseau
│   └── example_config.yaml      # Exemple de configuration
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
└── scripts/
    ├── install_deps.sh          # Installation dépendances système
    └── firstboot/               # Scripts firstboot pour VMs migrées
        ├── linux_firstboot.sh
        └── windows_firstboot.ps1
```

---

## 4. Détail des modules

### 4.1 Module VMware (`vmware/`)

#### 4.1.1 `client.py` — Connexion vCenter (Confiance: 95)

```python
# Logique inspirée de vjailbreak/v2v-helper/vcenter/vcenterops.go
class VSphereClient:
    """Connexion et authentification vSphere/vCenter."""

    def connect(self, host, username, password, insecure=False) -> ServiceInstance
    def disconnect(self)
    def get_thumbprint(self) -> str  # SHA-1 du certificat
    def get_datacenters(self) -> List[Datacenter]
    def get_clusters(self, datacenter) -> List[Cluster]
    def get_hosts(self, cluster) -> List[Host]
```

**Points d'attention** :
- Support SSL insecure (auto-signé) — fréquent en entreprise
- Retry avec backoff exponentiel (inspiré de vjailbreak)
- Session cache avec réauthentification automatique

#### 4.1.2 `inventory.py` — Inventaire VMs (Confiance: 92)

```python
@dataclass
class VMInfo:
    name: str
    uuid: str
    cpu: int
    memory_mb: int
    power_state: str
    guest_os: str           # "windows9Server64Guest", "ubuntu64Guest", etc.
    guest_os_full: str      # "Microsoft Windows Server 2019"
    firmware: str           # "bios" | "efi"
    disks: List[DiskInfo]
    nics: List[NICInfo]
    snapshots: List[str]
    tools_status: str       # "toolsOk", "toolsNotInstalled", etc.
    tools_version: str
    host: str               # ESXi host
    datacenter: str
    cluster: str

@dataclass
class DiskInfo:
    name: str
    size_gb: float
    thin_provisioned: bool
    datastore: str
    path: str               # "[datastore1] vm/vm.vmdk"
    controller_type: str    # "scsi", "nvme", "ide"

@dataclass
class NICInfo:
    mac_address: str
    network: str
    adapter_type: str       # "vmxnet3", "e1000", "e1000e"
    connected: bool
    ip_addresses: List[str]
```

**Collecte** : Utilise `PropertyCollector` de govmomi/pyvmomi pour une collecte efficace en batch plutôt que requête par VM.

#### 4.1.3 `export.py` — Export des disques (Confiance: 78)

Deux stratégies d'export :

**Stratégie A — OVF Export (simple mais lent)** :
```
VM → OVF Export → fichiers .vmdk locaux → conversion → upload
```
- ✅ Simple à implémenter
- ❌ Nécessite espace disque local = taille VM
- ❌ Temps de transfert doublé (download + upload)

**Stratégie B — NBD/VDDK Streaming (complexe mais optimal)** :
```
VM → NBD/VDDK → pipe → qemu-img convert → pipe → S3 upload
```
- ✅ Pas d'espace disque intermédiaire
- ✅ Streaming direct
- ❌ Nécessite VDDK (licence VMware)
- ❌ Plus complexe à implémenter

**Stratégie C — Hybride (recommandée)** :
```
VM → OVF Export → .vmdk local → conversion qcow2 → upload S3 → cleanup
```
- ✅ Fiable et bien supporté
- ✅ Permet reprise après erreur
- ❌ Nécessite espace disque local

**Recommandation** : Commencer par Stratégie C, puis implémenter B en v2.

#### 4.1.4 `snapshot.py` — Gestion snapshots (Confiance: 92)

```python
class SnapshotManager:
    def create_migration_snapshot(self, vm, name="vmware2scw-migration") -> str
    def delete_migration_snapshot(self, vm, name) -> None
    def list_snapshots(self, vm) -> List[SnapshotInfo]
    def cleanup_migration_snapshots(self, vm) -> None
```

---

### 4.2 Module Converter (`converter/`)

#### 4.2.1 `vmware_tools.py` — Nettoyage VMware Tools (Confiance: 85)

Utilise `virt-customize` ou `guestfish` pour :

**Linux** :
- Désinstaller `open-vm-tools` / `vmware-tools`
- Supprimer les services systemd VMware
- Nettoyer `/etc/vmware-tools/`
- Supprimer les modules kernel `vmw_*`, `vmxnet*`

**Windows** :
- Marquer VMware Tools pour désinstallation au prochain boot
- Supprimer les services VMware du registre
- Injecter un script firstboot pour compléter le nettoyage

```python
class VMwareToolsCleaner:
    def clean_linux(self, disk_path: str) -> None
    def clean_windows(self, disk_path: str) -> None
    def detect_os_family(self, disk_path: str) -> str  # "linux" | "windows"
```

#### 4.2.2 `virtio.py` — Injection drivers VirtIO (Confiance: 85)

```python
class VirtIOInjector:
    def inject_linux(self, disk_path: str) -> None
        # Linux : vérifier que les modules virtio sont dans initramfs
        # Régénérer initramfs si nécessaire (dracut, update-initramfs)

    def inject_windows(self, disk_path: str, virtio_iso: str) -> None
        # Windows : injecter les drivers depuis l'ISO virtio-win
        # viostor (storage), netkvm (network), vioserial, balloon, etc.
```

**Note critique** : Sans VirtIO, la VM ne pourra PAS démarrer sur KVM. C'est l'étape la plus risquée de la migration.

#### 4.2.3 `disk.py` — Conversion VMDK → qcow2 (Confiance: 95)

```python
class DiskConverter:
    def convert(self, input_path: str, output_path: str,
                compress: bool = True) -> None:
        """
        qemu-img convert -f vmdk -O qcow2 [-c] input.vmdk output.qcow2

        Options:
        - compress (-c) : réduit significativement la taille pour l'upload
        - preallocation=off : par défaut, évite l'allocation complète
        """

    def get_info(self, image_path: str) -> dict:
        """qemu-img info --output=json image"""

    def check(self, image_path: str) -> bool:
        """qemu-img check image.qcow2"""
```

#### 4.2.4 `bootloader.py` — Adaptation Bootloader (Confiance: 70 — ⚠️ SPÉCULATIF)

Problèmes potentiels :
- `/etc/fstab` avec références à des devices `/dev/sd*` qui changent en `/dev/vd*`
- GRUB configuré pour des devices spécifiques VMware
- Initramfs sans modules VirtIO

```python
class BootloaderAdapter:
    def fix_fstab(self, disk_path: str) -> None
        # Remplacer /dev/sda → /dev/vda si nécessaire
        # Préférer UUID si possible

    def fix_grub(self, disk_path: str) -> None
        # Mettre à jour GRUB pour les devices virtio

    def rebuild_initramfs(self, disk_path: str, os_family: str) -> None
        # dracut (RHEL/CentOS) ou update-initramfs (Debian/Ubuntu)
```

#### 4.2.5 `network.py` — Adaptation Réseau (Confiance: 70 — ⚠️ SPÉCULATIF)

```python
class NetworkAdapter:
    def adapt_linux(self, disk_path: str, new_mac: str = None) -> None
        # Supprimer les règles udev persistantes (70-persistent-net.rules)
        # Adapter netplan / ifcfg-* / NetworkManager
        # vmxnet3 → virtio-net (changement de nom d'interface potentiel)

    def adapt_windows(self, disk_path: str) -> None
        # Pas besoin d'adaptation manuelle si virtio-win est injecté
        # Le driver netkvm prendra le relais automatiquement
```

---

### 4.3 Module Scaleway (`scaleway/`)

#### 4.3.1 `s3.py` — Upload S3 (Confiance: 92)

```python
class ScalewayS3:
    def __init__(self, region: str, access_key: str, secret_key: str):
        self.client = boto3.client('s3',
            endpoint_url=f"https://s3.{region}.scw.cloud",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region
        )

    def upload_image(self, local_path: str, bucket: str, key: str,
                     callback=None) -> str:
        """Multipart upload avec progress callback."""

    def create_bucket_if_not_exists(self, bucket: str) -> None

    def delete_image(self, bucket: str, key: str) -> None

    def get_image_url(self, bucket: str, key: str) -> str
```

#### 4.3.2 `instance.py` — API Scaleway Instance (Confiance: 80)

**Processus d'import d'image Scaleway** (Confiance: 75 — ⚠️ SPÉCULATIF sur certains détails API) :

```
1. Upload qcow2 → S3 Scaleway
2. Créer un snapshot depuis l'URL S3 :
   POST /instance/v1/zones/{zone}/snapshots
   {
     "name": "migration-{vm_name}",
     "volume_type": "b_ssd" | "l_ssd",
     "bucket": "bucket-name",
     "key": "image.qcow2"
   }
3. Créer une image depuis le snapshot :
   POST /instance/v1/zones/{zone}/images
   {
     "name": "migration-{vm_name}",
     "root_volume": snapshot_id,
     "arch": "x86_64"
   }
4. Créer une instance depuis l'image :
   POST /instance/v1/zones/{zone}/servers
   {
     "name": "migrated-{vm_name}",
     "commercial_type": "PRO2-S",
     "image": image_id,
     ...
   }
```

```python
class ScalewayInstanceAPI:
    def create_snapshot_from_s3(self, zone, name, bucket, key,
                                 volume_type="b_ssd", size=None) -> dict
    def wait_for_snapshot(self, zone, snapshot_id, timeout=600) -> dict
    def create_image(self, zone, name, snapshot_id, arch="x86_64") -> dict
    def create_instance(self, zone, name, image_id,
                         commercial_type, tags=None) -> dict
    def list_instance_types(self, zone) -> List[dict]
```

#### 4.3.3 `mapping.py` — Mapping des ressources (Confiance: 82)

```python
class ResourceMapper:
    def suggest_instance_type(self, vm_info: VMInfo) -> List[InstanceTypeSuggestion]:
        """
        Algorithme de matching :
        1. Filtrer par vCPU >= vm.cpu
        2. Filtrer par RAM >= vm.memory_mb
        3. Filtrer par stockage max >= somme(disques)
        4. Trier par "meilleur fit" (plus petit type suffisant)
        5. Retourner top 3 suggestions avec prix estimé
        """

    def map_network(self, vmware_nics: List[NICInfo],
                     scaleway_vpcs: List[dict]) -> dict

    def map_storage(self, vmware_disks: List[DiskInfo]) -> dict
        # local SSD (l_ssd) vs block SSD (b_ssd)
```

#### 4.3.4 `types.py` — Catalogue instances Scaleway (Confiance: 88)

```yaml
# configs/mapping_templates/instance_types.yaml
instance_types:
  DEV1-S:
    vcpus: 2
    ram_gb: 2
    local_storage_gb: 20
    max_volumes: 1
    bandwidth_mbps: 200

  DEV1-M:
    vcpus: 3
    ram_gb: 4
    local_storage_gb: 40
    max_volumes: 1
    bandwidth_mbps: 300

  GP1-S:
    vcpus: 8
    ram_gb: 32
    local_storage_gb: 150
    max_volumes: 16
    bandwidth_mbps: 500

  PRO2-S:
    vcpus: 2
    ram_gb: 8
    local_storage_gb: 0
    block_storage: true
    max_volumes: 16
    bandwidth_mbps: 250

  PRO2-M:
    vcpus: 4
    ram_gb: 16
    local_storage_gb: 0
    block_storage: true
    max_volumes: 16
    bandwidth_mbps: 500

  # ... etc.
```

---

### 4.4 Module Pipeline (`pipeline/`)

#### 4.4.1 `migration.py` — Pipeline principal (Confiance: 88)

```python
class MigrationPipeline:
    """
    Orchestrateur principal de la migration.
    Chaque étape est idempotente et peut être reprise.
    """

    STAGES = [
        "validate",           # Pré-validation compatibilité
        "snapshot",           # Création snapshot source
        "export",             # Export disques VMDK
        "clean_tools",        # Nettoyage VMware tools
        "inject_virtio",      # Injection drivers VirtIO
        "convert",            # Conversion VMDK → qcow2
        "fix_bootloader",     # Adaptation bootloader
        "fix_network",        # Adaptation réseau
        "upload_s3",          # Upload vers S3 Scaleway
        "import_scaleway",    # Import image Scaleway
        "verify",             # Validation post-migration
        "cleanup",            # Nettoyage ressources transitoires
    ]

    async def run(self, vm_name: str, config: MigrationConfig) -> MigrationResult
    async def resume(self, migration_id: str) -> MigrationResult
    async def rollback(self, migration_id: str) -> None
```

#### 4.4.2 `state.py` — Gestion d'état (Confiance: 85)

```python
class MigrationState:
    """
    Persiste l'état de chaque migration dans un fichier JSON local.
    Permet la reprise après erreur.
    """
    migration_id: str
    vm_name: str
    current_stage: str
    completed_stages: List[str]
    artifacts: dict  # Chemins fichiers temporaires, IDs ressources, etc.
    started_at: datetime
    error: Optional[str]
```

#### 4.4.3 `validator.py` — Pré-validation (Confiance: 80)

```python
class MigrationValidator:
    def validate(self, vm_info: VMInfo, target_type: str) -> ValidationReport:
        checks = [
            self.check_os_supported,
            self.check_disk_size_limits,
            self.check_firmware_compatibility,  # UEFI vs BIOS
            self.check_no_rdm_disks,           # Raw Device Mapping non supporté
            self.check_no_shared_disks,         # Disques partagés non supporté
            self.check_no_pci_passthrough,      # GPU passthrough non supporté
            self.check_snapshot_space,           # Espace pour snapshot
            self.check_tools_status,            # VMware Tools installé ?
        ]
        # ...
```

---

## 5. Workflow CLI

### 5.1 Commandes principales

```bash
# Connexion et inventaire
vmware2scw inventory --vcenter vcenter.example.com \
                     --username admin@vsphere.local \
                     --password-file /path/to/password \
                     --output inventory.json

# Validation d'une VM
vmware2scw validate --vm "web-server-01" \
                    --target-type PRO2-S \
                    --config migration.yaml

# Migration individuelle
vmware2scw migrate --vm "web-server-01" \
                   --target-type PRO2-S \
                   --zone fr-par-1 \
                   --config migration.yaml

# Migration batch
vmware2scw migrate-batch --plan migration-plan.yaml

# Vérifier l'état d'une migration
vmware2scw status --migration-id abc123

# Reprendre une migration échouée
vmware2scw resume --migration-id abc123

# Rollback
vmware2scw rollback --migration-id abc123
```

### 5.2 Fichier de configuration

```yaml
# migration.yaml
vmware:
  vcenter: vcenter.example.com
  username: admin@vsphere.local
  password_env: VCENTER_PASSWORD  # Variable d'environnement
  insecure: true                  # SSL auto-signé

scaleway:
  access_key_env: SCW_ACCESS_KEY
  secret_key_env: SCW_SECRET_KEY
  organization_id: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  project_id: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
  default_zone: fr-par-1
  s3_region: fr-par
  s3_bucket: vmware-migration-transit

conversion:
  work_dir: /var/lib/vmware2scw/work
  compress_qcow2: true
  virtio_win_iso: /path/to/virtio-win.iso  # Pour Windows
  cleanup_on_success: true

migration:
  parallel_exports: 2
  parallel_uploads: 3
  retry_count: 3
  retry_delay_seconds: 30
```

### 5.3 Plan de migration batch

```yaml
# migration-plan.yaml
migrations:
  - vm_name: web-server-01
    target_type: PRO2-S
    zone: fr-par-1
    network_mapping:
      "VM Network": vpc-xxxxxxxx
    priority: 1

  - vm_name: db-server-01
    target_type: PRO2-M
    zone: fr-par-1
    tags:
      - database
      - production
    priority: 2

  - vm_name: app-server-*     # Wildcard
    target_type: PRO2-S
    zone: fr-par-2
    priority: 3
```

---

## 6. Considérations de sécurité

| Aspect | Approche | Confiance |
|--------|----------|-----------|
| Credentials VMware | Variables d'environnement ou fichier protégé (0600) | 90 |
| Credentials Scaleway | Variables d'environnement ou fichier protégé | 90 |
| Transmission réseau | TLS systématique (vCenter, S3) | 95 |
| Fichiers temporaires | Répertoire avec permissions restreintes, nettoyage automatique | 88 |
| Logs | Masquage des mots de passe et tokens | 90 |
| Données disque | Transit chiffré, pas de stockage persistant non nécessaire | 85 |

---

## 7. Gestion d'erreurs et reprise

### 7.1 Idempotence

Chaque étape du pipeline est conçue pour être idempotente :
- **Snapshot** : Si le snapshot existe déjà, le réutiliser
- **Export** : Si le fichier VMDK existe et a la bonne taille, skip
- **Convert** : Si le fichier qcow2 existe et est valide, skip
- **Upload** : Support du multipart upload avec reprise
- **Import Scaleway** : Vérifier si le snapshot/image existe déjà

### 7.2 Stratégie de retry

```python
@retry(
    max_attempts=3,
    backoff=exponential(base=2, max_delay=60),
    retryable_exceptions=[ConnectionError, TimeoutError, APIRateLimitError]
)
```

---

## 8. Containerisation

### 8.1 Dockerfile

```dockerfile
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    python3 python3-pip \
    qemu-utils \
    libguestfs-tools \
    nbdkit \
    libguestfs-dev \
    linux-image-generic \     # Nécessaire pour libguestfs
    && rm -rf /var/lib/apt/lists/*

# Drivers VirtIO Windows (optionnel)
ADD https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso \
    /opt/virtio-win/virtio-win.iso

COPY . /app
WORKDIR /app
RUN pip install --break-system-packages -e .

ENTRYPOINT ["vmware2scw"]
```

---

## 9. Roadmap

### v0.1.0 — MVP
- [ ] Connexion vCenter / inventaire
- [ ] Export VMDK (stratégie C — local)
- [ ] Conversion VMDK → qcow2
- [ ] Nettoyage VMware tools (Linux uniquement)
- [ ] Upload S3 Scaleway
- [ ] Import image basique

### v0.2.0 — Production-ready
- [ ] Support Windows (VMware tools cleanup + VirtIO injection)
- [ ] Adaptation bootloader et réseau
- [ ] Gestion d'état et reprise
- [ ] Migration batch
- [ ] Validation post-migration

### v0.3.0 — Optimisations
- [ ] Streaming NBD/VDDK (in-fly)
- [ ] Conversion parallèle multi-disques
- [ ] Interface web (FastAPI + React)
- [ ] Support CBT (Changed Block Tracking) pour migration incrémentale

### v1.0.0 — Enterprise
- [ ] Support multi-zone Scaleway
- [ ] Cutover automatisé (arrêt VM source → sync final → démarrage cible)
- [ ] Intégration Terraform (import state)
- [ ] Rapports de migration PDF

---

## 10. Risques et limitations connues

| Risque | Impact | Probabilité | Mitigation |
|--------|--------|-------------|------------|
| VM Windows avec UEFI Secure Boot | Boot impossible | Moyen | Désactiver Secure Boot avant migration |
| Disques RDM (Raw Device Mapping) | Non convertible | Faible | Détecter et exclure en pré-validation |
| VM avec snapshots complexes | Export corrompu | Moyen | Consolider les snapshots avant migration |
| Licences Windows liées au hardware | Activation perdue | Élevé | Informer l'utilisateur, planifier réactivation |
| Applications VMware-dépendantes | Dysfonctionnement post-migration | Moyen | Audit applicatif pré-migration |
| Latence réseau sur gros disques | Timeout upload | Moyen | Multipart upload avec retry |
| Limites API Scaleway (rate limiting) | Migration lente | Faible | Backoff exponentiel, file d'attente |

---

## 11. Comparaison avec vjailbreak

| Aspect | vjailbreak | vmware2scw |
|--------|-----------|------------|
| Cible | OpenStack | Scaleway Instances |
| Format disque | qcow2 (via Cinder) | qcow2 (via S3) |
| Langage | Go | Python |
| Déploiement | Kubernetes (k3s) | CLI / Docker |
| Copie disque | NBD/VDDK (streaming) | OVF Export (v1) → NBD (v2) |
| Conversion | virt-v2v-in-place | virt-v2v + qemu-img |
| UI | Web (React) | CLI (Rich) → Web (v3) |
| Migration chaude | Oui (CBT) | Non (v1) → Oui (v3) |

---

*Ce document sera mis à jour au fil du développement.*
