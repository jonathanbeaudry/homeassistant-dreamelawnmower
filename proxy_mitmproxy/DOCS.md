# Proxy mitmproxy

Proxy HTTPS basé sur l'image **officielle** `mitmproxy/mitmproxy` (12.2.2), pour inspecter
le trafic d'un appareil du réseau local (téléphone, objet connecté).

## Utilisation

1. Démarrer l'add-on (démarrage manuel : il ne tourne que lorsqu'on en a besoin).
2. Sur l'appareil : Wi-Fi → proxy **manuel** → hôte = IP de Home Assistant, port **8080**.
3. Sur l'appareil, ouvrir **http://mitm.it** et installer le certificat. Il est conservé
   dans `/data/mitmproxy` : pas besoin de le réinstaller après un redémarrage.
4. Interface web : `http://<IP de HA>:8081` (mot de passe = option `mot_de_passe_web` ;
   sans mot de passe, voir le journal de l'add-on).
5. **Arrêter l'add-on et retirer le proxy de l'appareil** à la fin.

## Options

| Option | Rôle |
|---|---|
| `sauvegarder` | Enregistrer tout le trafic capturé dans `fichier` (format mitmproxy, ajouté à la suite) |
| `fichier` | Chemin de la capture, sous `/share` |
| `mot_de_passe_web` | Mot de passe de l'interface web (facultatif) |
| `arguments` | Arguments supplémentaires passés à `mitmweb` (ex. `--set ignore_hosts=...`) |

⚠️ Les ports 8080 et 8081 sont publiés sur l'hôte Home Assistant : ne jamais les
rediriger depuis le routeur vers Internet, sinon le proxy devient public.

Note : depuis Android 7, une app n'accepte un certificat installé par l'utilisateur que si
elle l'autorise (`network_security_config`) ; sinon il faut une version de l'app modifiée.
