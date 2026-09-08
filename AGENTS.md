# Instructions agent

Ce fichier documente les actions a effectuer quand une modification est validee.

Pour chaque modification de code :

1. Ajouter ou mettre a jour les commentaires utiles en francais.
2. Ajouter ou mettre a jour la documentation pertinente en francais.
3. Eviter les commentaires redondants qui repètent simplement le code.

Quand l'utilisateur dit "je valide" ou "jke valide" :

1. Mettre a jour `CHANGELOG.md` en ajoutant une entree en anglais immediatement sous le titre
   `# Changelog`, avant la premiere section de version et sans creer de titre `Non publie`.
2. Resumer les changements qui viennent d'etre valides.
3. Stager les fichiers modifies pertinents avec `git add`.
4. Creer immediatement le commit avec un message en anglais qui commence par un gitmoji adapte,
   sans se contenter de proposer le message.
5. Ne pas pousser le commit.

Garder l'entree de changelog en anglais, concise et centree sur les changements visibles ou
importants pour le projet.
