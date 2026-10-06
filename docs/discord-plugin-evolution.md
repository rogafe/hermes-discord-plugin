# Pistes d’évolution du plugin Discord Hermes

État observé dans ce dépôt le 6 octobre 2026. Cette note croise l’API Discord officielle avec `__init__.py`, `embeds.py`, `render.py`, `plugin.yaml` et `README.md`. Elle propose une feuille de route, sans modifier le code du plugin.

## Architecture actuelle

Le plugin est un adaptateur local `discord.py` : il mémorise le modèle via `post_llm_call`, récupère le bot et l’adaptateur via `register_platform_handler`, puis enveloppe `send` et `edit_message`. Il convertit les réponses finales en embeds et reconnaît une seule forme de `Cronjob Response`, qu’il découpe en intro, offres numérotées et résumé. Les cartes d’offre ajoutent trois boutons. Les clics passent par le Gateway `on_interaction`, sont soumis au contrôle d’autorisation Hermes et injectent le choix en texte dans la conversation. L’état anti-double-clic (`claimed_clicks`) est seulement en mémoire.

## Capacités Discord utiles

- **Messages enrichis** : un message peut porter jusqu’à 10 embeds, avec une limite globale de 6 000 caractères pour les champs textuels des embeds. Le contenu texte a une limite de 2 000 caractères. Discord impose un numéro de version dans les routes HTTP (`/api/v{version}`) ; les réponses restent soumises aux limites REST. Cela justifie de garder un découpage propre aux cartes et d’ajouter un constructeur commun qui tronque ou fragmente explicitement les longs contenus. [Référence API](https://docs.discord.com/developers/reference) · [Ressource Message et limites des embeds](https://docs.discord.com/developers/resources/message)
- **Composants et interactions** : boutons, menus de sélection et modals permettent de collecter respectivement une action, un choix structuré ou du texte libre. Une modal s’ouvre en réponse à une commande ou à un composant. Un clic envoie une interaction avec `custom_id`; le bot doit l’acquitter dans les 3 secondes. Le token d’interaction sert aux réponses de suivi pendant 15 minutes. Le Gateway permet de recevoir ces interactions sur la même connexion déjà utilisée pour les messages; un endpoint HTTP dédié est l’autre modèle possible. [Interactions](https://docs.discord.com/developers/interactions/overview) · [Réception et réponses](https://docs.discord.com/developers/interactions/receiving-and-responding) · [Composants de message](https://docs.discord.com/developers/components/using-message-components) · [Modals](https://docs.discord.com/developers/components/using-modal-components)
- **Slash commands et commandes contextuelles** : les commandes d’application sont créées par HTTP. Les commandes de serveur se mettent à jour immédiatement et sont recommandées pour les essais; les commandes globales conviennent à la diffusion générale. Les commandes de contexte permettent d’agir sur un message ou un utilisateur sans parser le texte. [Commandes d’application](https://docs.discord.com/developers/docs/interactions/slash-commands)
- **Webhooks** : les webhooks entrants sont un canal simple pour publier des messages dans un salon; ils ne fournissent pas à eux seuls un flux entrant pour les clics. L’application doit continuer de recevoir les interactions via Gateway ou ajouter un endpoint HTTP d’interactions. Si on choisit HTTP, Discord exige de vérifier la signature de chaque requête. [Vue d’ensemble des interactions](https://docs.discord.com/developers/interactions/overview) · [Réception et réponses](https://docs.discord.com/developers/interactions/receiving-and-responding)
- **Intents et permissions** : les intents contrôlent les familles d’événements Gateway. Lire le texte des messages utilisateurs peut exiger l’intent privilégié `MESSAGE_CONTENT`, activé dans le portail et soumis à approbation pour les applications concernées. Un flux centré sur les interactions évite d’ajouter cet intent juste pour traiter des boutons; le plugin doit toutefois respecter les intents configurés par Hermes. [Gateway et intents](https://docs.discord.com/developers/events/gateway) · [Bots](https://docs.discord.com/developers/bots/overview)
- **Reconnexion et limites de débit** : le Gateway peut reprendre une session interrompue; l’application doit traiter les redémarrages complets séparément. Les limites REST sont par route et globales : utiliser les en-têtes de rate-limit et `retry_after`, éviter les rafales d’éditions et traiter les réponses d’erreur. [Gateway](https://docs.discord.com/developers/events/gateway) · [Rate limits](https://docs.discord.com/developers/topics/rate-limits)

## Évolutions proposées

### 1. Rendre la livraison interactive fiable

Garder le Gateway partagé avec Hermes, mais formaliser un routeur de composants unique. Accuser immédiatement le clic (réponse éphémère ou defer), puis exécuter l’action Hermes et mettre à jour la carte : choix sélectionné, action horodatée, contrôles désactivés ou bouton « Annuler/modifier ». Valider `custom_id`, salon/thread, auteur du message, job et numéro contre un enregistrement connu, pas seulement contre le texte du message. Rendre l’action idempotente afin que double-clic, retransmission Gateway ou redémarrage ne crée pas plusieurs décisions.

L’état qui lie `job_id`, numéro d’offre, message Discord et conversation Hermes devrait être durable (petite base SQLite ou stockage fourni par Hermes). Aujourd’hui les `custom_id` gardent job/numéro/action, mais `claimed_clicks` disparaît au redémarrage. Les interactions Discord ne sont pas une file d’attente durable pour les décisions métier; répondre rapidement et persister avant l’accusé évite de perdre un choix. Garder un fallback textuel si la livraison interactive est indisponible.

### 2. Séparer le parseur du rendu Discord

Remplacer le parseur regex focalisé sur une seule mise en page par un modèle interne : rapport, sections, items/actionnables, pièces jointes et liens. Ajouter des parseurs explicites pour les rapports d’offres, listes de tâches, sondages, confirmations, résumés et alertes; en absence de format reconnu, rendre le message normalement. Assurer que l’envoi conserve la réponse logique attendue par Hermes lorsque le plugin transforme un message en plusieurs cartes. Faire un envoi natif avec embed/components si la couture d’adaptateur le permet, au lieu du flash texte puis édition actuellement documenté.

### 3. Étendre les interactions selon le cas d’usage

- Une **sélection déroulante** convient aux longues listes d’actions/éléments, où plusieurs séries de boutons par carte encombreraient le salon.
- Une **modal** permet de demander une remarque, une raison de rejet, une date ou un complément sans obliger à écrire un message séparé.
- Des **slash commands** comme `/hermes status`, `/hermes stop`, `/hermes cron list`, `/hermes cron run` et `/hermes config` offrent des commandes découvrables et des entrées typées. Démarrer dans un serveur de test, puis enregistrer globalement si désiré.
- Une **commande contextuelle de message** peut proposer « Envoyer à Hermes », « Résumer » ou « Continuer dans ce thread » sur n’importe quel message.
- Un **mode pagination** peut regrouper les résultats et réduire le nombre de messages, avec boutons précédent/suivant et état de page. Les composants par message ont un budget de structure limité; éviter trois boutons pour chacune de dizaines d’offres.

### 4. Améliorer la présentation et l’exploitation

Ajouter des thèmes/configurations par conversation, langue et densité; rendre uniformes les titres, champs, liens, statuts, timestamps et mentions. Prévoir une limite configurable d’items par carte/message, un résumé et une pagination pour les résultats abondants. Ajouter un mode diagnostic qui indique dans les logs pourquoi une carte ou ses composants n’ont pas été ajoutés, sans journaliser jetons, contenu privé ou données sensibles. Exposer des réglages indépendants pour embeds, cartes d’offres, boutons, slash commands et boutons de confirmation.

## Priorités et critères d’acceptation

1. **P0 — robustesse des offres** : test des formats incomplets et des messages multipart; clic accepté/refusé; action acquittée en moins de 3 s; clic répété sans effet métier doublé; action routée vers le bon thread; reprise après redémarrage avec confirmation éditée ou état expiré clairement signalé.
2. **P1 — interaction générique** : sélecteur/modal avec réponse éphémère; pagination; tests de limites d’embed et de composants; aucune action interactive affichée quand le routeur entrant est indisponible.
3. **P2 — contrôle Hermes depuis Discord** : commandes de statut, cron et gestion de conversation, installables d’abord à l’échelle d’un serveur de test.

Les limites Discord changent avec l’API. Le plugin devrait s’appuyer sur les constantes et validations de `discord.py`, documenter la version d’API et traiter proprement les erreurs HTTP, plutôt que dupliquer des nombres dispersés dans le code.
