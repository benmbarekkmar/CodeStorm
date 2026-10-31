# HackFlow

<p align="center">
  <strong>HackFlow — Plateforme de gestion des hackathons étudiants</strong><br />
  Centralisez les hackathons, les équipes, les projets, les évaluations et les prix.
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" />
  <img alt="Django" src="https://img.shields.io/badge/Django-5.2-092E20?logo=django&logoColor=white" />
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-Database-4169E1?logo=postgresql&logoColor=white" />
  <img alt="Status" src="https://img.shields.io/badge/Status-En%20d%C3%A9veloppement-orange" />
</p>

---

## Sommaire

- [Présentation](#présentation)
- [Notre_équipe](#Notre_équipe)
- [Fonctionnalités](#fonctionnalités)
- [Rôles](#rôles)
- [Modules](#modules)
- [Technologies](#technologies)
- [Architecture](#architecture)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [Configuration](#configuration)
- [Base de données et migrations](#base-de-données-et-migrations)
- [Lancement](#lancement)
- [Tests](#tests)
- [Analyse assistée par IA](#analyse-assistée-par-ia)
- [Données de démonstration](#données-de-démonstration)
- [Sécurité](#sécurité)

## Présentation

**HackFlow** est une application web Django conçue pour faciliter l'organisation et le suivi des hackathons étudiants. Elle regroupe dans un même espace la gestion des événements, la constitution des équipes, la soumission des projets, les évaluations du jury et l'attribution des prix.

L'application distingue plusieurs rôles — participant, organisateur et jury — dont les permissions doivent respecter leurs responsabilités. Le module d'analyse assistée par IA fournit des indications complémentaires pour l'examen des projets ; il ne remplace pas le jugement du jury.

## Application déployée

HackFlow est déployée sur **Render**, ce qui permet d'accéder à l'application en ligne sans avoir à installer le projet localement.

- **Plateforme de déploiement :** Render
- **Lien de l'application :** [Accéder à HackFlow](URL_DE_VOTRE_APPLICATION)



## Notre_équipe

Nous sommes une équipe de cinq étudiants travaillant ensemble au développement de **HackFlow**, une plateforme dédiée à la gestion des hackathons étudiants.

<p align="center">
  <img src="docs/images/equipe-hackflow.png" alt="Équipe de développement HackFlow" width="100%">
</p>

Chaque membre contribue au développement d’un module spécifique, avec une authentification commune à l’ensemble du projet.


## Fonctionnalités

- Gestion des hackathons, des dates et des challenges.
- Création d'équipes, adhésions, invitations et gestion des membres.
- Gestion des projets : description, technologies, dépôt GitHub et soumission.
- Évaluations par critères, notes et commentaires.
- Catégories de prix, calcul du classement et attribution des prix.
- Gestion et consultation des ressources.
- Authentification et contrôle d'accès selon le rôle.
- Rapports d'analyse assistée par IA, lorsque le service est configuré.
- Tableaux de bord et suivi des informations selon les permissions.

Les fonctions exactes dépendent du code présent dans la version du dépôt.

## Rôles

| Rôle | Responsabilités principales |
|---|---|
| **Participant** | Consulter les hackathons, rejoindre ou créer une équipe selon les règles, soumettre un projet et consulter les informations autorisées. |
| **Organisateur** | Gérer les hackathons de son périmètre et suivre les équipes, projets et analyses autorisés. |
| **Jury** | Consulter les projets qui lui sont accessibles, réaliser les évaluations et utiliser les informations complémentaires disponibles. |

Les permissions doivent être contrôlées côté serveur. Masquer un bouton dans l'interface ne remplace pas une vérification d'autorisation dans les vues ou services Django.

## Modules

| Module | Objectif |
|---|---|
| Authentification | Comptes, connexion et contrôle des rôles. |
| Hackathons | Événements, dates, challenges et inscriptions. |
| Équipes | Création, adhésion, invitations et membres. |
| Projets | Description, technologies, dépôt et soumission. |
| Évaluations | Critères, notes, commentaires et suivi. |
| Prix | Catégories, classement et attributions. |
| Ressources | Ressources utiles aux participants. |
| Analyses IA | Rapports et suggestions d'aide à l'évaluation. |

## Technologies

- Python 3.11
- Django 5.2
- PostgreSQL
- HTML, CSS et JavaScript
- API Gemini pour l'analyse IA, si configurée

La liste exacte des dépendances est définie par les fichiers de dépendances du dépôt (`requirements.txt`, `pyproject.toml` ou équivalent). Consultez-les avant d'installer ou de mettre à jour des packages.

## Architecture

HackFlow suit l'architecture web Django : les URL dirigent les requêtes vers les vues, qui utilisent les modèles et services métier avant de renvoyer une page ou une réponse adaptée.

Arborescence indicative — les noms exacts peuvent varier :

```text
HackFlow/
├── manage.py
├── config/          # Configuration Django et routage principal
├── accounts/        # Comptes et authentification
├── hackathons/      # Gestion des hackathons
├── teams/           # Équipes et adhésions
├── projects/        # Projets et soumissions
├── evaluations/     # Critères et évaluations
├── prizes/          # Classements et prix
├── resources/       # Ressources
├── analyses/        # Rapports IA
├── templates/       # Templates HTML
├── static/          # CSS, JavaScript et images
├── media/           # Fichiers téléversés, si configuré
├── requirements.txt # Dépendances, si présent
└── .env             # Variables locales — ne jamais committer
```

Vérifiez le contenu réel du dépôt pour connaître son arborescence exacte.

## Prérequis

- Python 3.11 ou version compatible avec les dépendances.
- Git.
- PostgreSQL si la configuration Django l'utilise.
- Une clé API Gemini uniquement pour lancer les analyses IA.
- PowerShell sous Windows ou un terminal équivalent sous Linux/macOS.

## Installation

### 1. Cloner le dépôt

Remplacez l'URL par celle du dépôt officiel :

```bash
git clone <URL_DU_DEPOT>
cd HackFlow
```

### 2. Créer et activer l'environnement virtuel

**Windows — PowerShell :**

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
```

Si l'activation PowerShell est bloquée, utilisez directement l'interpréteur de l'environnement :

```powershell
.\venv\Scripts\python.exe -m pip install --upgrade pip
```

**Linux / macOS :**

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Installer les dépendances

Si le dépôt contient `requirements.txt` :

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Si le projet utilise un autre gestionnaire, suivez le fichier de configuration correspondant.

## Configuration

Créez un fichier `.env` à partir de `.env.example` s'il existe, ou définissez les variables dans l'environnement. **Ne placez jamais de vraies clés API ou de mots de passe dans Git.**

Exemple indicatif — les noms réellement attendus doivent être confirmés dans `settings.py` et le service IA :

```dotenv
DJANGO_SECRET_KEY=replace-with-a-unique-secret
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost

# Exemple de configuration PostgreSQL
DB_NAME=hackflow
DB_USER=your_database_user
DB_PASSWORD=your_database_password
DB_HOST=127.0.0.1
DB_PORT=5432

# Facultatif : analyse IA
GEMINI_API_KEY=
GEMINI_MODEL=
GEMINI_TIMEOUT=120
```

Les noms peuvent différer dans le projet (par exemple `SECRET_KEY`, `DEBUG`, `DATABASE_URL` ou `LLM_TIMEOUT`). Vérifiez le code avant de configurer ces variables. Augmenter le délai ne résout pas nécessairement la cause d'un timeout.

### Configuration de sécurité

- Utilisez une clé Django secrète et unique.
- Gardez `DEBUG=False` en production.
- Configurez `ALLOWED_HOSTS` avec les domaines attendus.
- Ne partagez jamais `.env`, les dumps de base ou les logs sensibles.
- Ne désactivez pas TLS pour contourner un problème de connexion.

## Base de données et migrations

Après configuration de la base :

```bash
python manage.py check
python manage.py showmigrations
python manage.py migrate
```

**Attention :** `migrate` modifie le schéma de la base ciblée. Vérifiez la configuration et l'environnement avant de l'exécuter, surtout pour une base partagée ou réelle.

Pour examiner le SQL d'une migration sans l'appliquer :

```bash
python manage.py sqlmigrate <app> <migration>
```

Pour vérifier si des migrations sont nécessaires :

```bash
python manage.py makemigrations --check --dry-run
```

Ne supprimez pas et ne réinitialisez pas une base partagée pour résoudre une erreur. Prenez une sauvegarde vérifiée avant toute opération importante.

## Lancement

Dans l'environnement virtuel activé :

```bash
python manage.py runserver
```

Ouvrez [http://127.0.0.1:8000/](http://127.0.0.1:8000/).

Le serveur de développement Django est destiné au développement local, pas à la production.

## Tests

Vérifications de base :

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

Pour tester des applications spécifiques :

```bash
python manage.py test projects
python manage.py test evaluations
python manage.py test prizes
python manage.py test analyses
```

Les commandes exactes dépendent de la configuration du projet. Si Django signale qu'une base de test existe déjà et demande confirmation avant sa suppression ou sa recréation, lisez le message attentivement avant de répondre.

Avant une livraison, vérifiez les permissions par rôle, les relations entre les modules, la cohérence des classements et le comportement des erreurs IA.

## Analyse assistée par IA

Le module `analyses` peut produire des rapports destinés à aider l'examen des projets. Son fonctionnement dépend de la configuration du fournisseur IA, de l'accessibilité du dépôt et de la récupération des fichiers.

Avant d'utiliser l'analyse IA, vérifiez que :

1. La clé API est configurée et n'apparaît jamais dans les logs.
2. Le modèle indiqué est pris en charge par le compte et l'API utilisés.
3. Le dépôt est accessible et son contenu est récupéré correctement.
4. Les timeouts HTTP et IA sont cohérents.
5. Les erreurs sont affichées comme des erreurs, pas comme des analyses réussies.
6. Un rapport est lié au bon projet uniquement si cette relation est établie.
7. Les permissions de consultation et de lancement respectent le périmètre de l'organisateur et du jury.

Les analyses IA sont une **aide à la décision**. Elles ne remplacent pas l'évaluation humaine, ne doivent pas modifier automatiquement les notes du jury et ne doivent pas publier automatiquement un classement.

En cas de timeout, examinez les logs et la configuration avant de relancer plusieurs appels, afin d'éviter une consommation inutile de quota.

## Données de démonstration

Si une commande de seed existe, consultez d'abord son aide :

```bash
python manage.py help seed_demo_data
```

Si l'option `--dry-run` est effectivement prise en charge :

```bash
python manage.py seed_demo_data --dry-run
```

Ne supposez pas qu'une option existe sans vérifier `--help`. Bonnes pratiques :

- tester d'abord sur une base isolée ;
- prendre et vérifier une sauvegarde avant toute insertion sur une base partagée ;
- utiliser des identifiants stables et une stratégie idempotente ;
- ne pas écraser les données saisies par les utilisateurs ;
- ne pas utiliser une option de suppression sans en comprendre le périmètre ;
- garder les résultats non publiés jusqu'à validation.

## Sécurité

- Vérifier les permissions côté serveur pour chaque action et chaque objet.
- Valider les données des formulaires et des requêtes.
- Protéger les clés API, mots de passe, cookies et variables secrètes.
- Limiter les URL et fichiers externes utilisés par l'analyse IA afin de réduire les risques SSRF et d'accès non autorisé.
- Ne jamais exécuter le code provenant d'un dépôt analysé.
- Éviter les données personnelles et les secrets dans les logs.
- Utiliser HTTPS et une configuration adaptée en production.
- Sauvegarder et vérifier les bases avant les modifications importantes.


### Timeout Gemini

- Vérifiez la valeur effective du timeout et le modèle configuré.
- Consultez les logs sans exposer la clé API.
- Vérifiez l'accès réseau, la taille du contexte et les délais du client HTTP.
- Évitez les tentatives automatiques répétées.
- Ne marquez pas une analyse comme réussie si aucun résultat n'a été reçu et enregistré.

### Connexion PostgreSQL

- Vérifiez que PostgreSQL est démarré.
- Vérifiez le nom de la base, l'hôte, le port et les identifiants.
- Confirmez que Django cible l'environnement voulu avant toute migration ou commande de seed.



