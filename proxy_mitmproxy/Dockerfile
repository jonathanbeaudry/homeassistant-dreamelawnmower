FROM mitmproxy/mitmproxy:12.2.2

COPY run.py /run.py

# L'image officielle passe par un entrypoint qui change d'utilisateur ; l'add-on
# doit pouvoir écrire dans /data (certificat) et /share (captures), donc on le court-circuite.
ENTRYPOINT []
CMD ["python3", "/run.py"]
