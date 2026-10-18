"""
Gera as chaves VAPID para notificações push. Rodar UMA vez:
    python gen_vapid.py
Copie as duas linhas para o .env e para as Variables do Railway
(na ADMIN e no CATÁLOGO — as mesmas chaves nos dois).
Se trocar as chaves, os aparelhos precisam ativar as notificações de novo.
"""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

key = ec.generate_private_key(ec.SECP256R1())
private_raw = key.private_numbers().private_value.to_bytes(32, "big")
public_raw = key.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
)
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()

print(f"VAPID_PRIVATE_KEY={b64(private_raw)}")
print(f"VAPID_PUBLIC_KEY={b64(public_raw)}")
