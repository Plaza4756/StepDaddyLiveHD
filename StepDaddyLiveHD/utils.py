import os
import re
import base64
import json
import struct
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

key_bytes = os.urandom(64)


def encrypt(input_string: str):
    input_bytes = input_string.encode()
    result = xor(input_bytes)
    return base64.urlsafe_b64encode(result).decode().rstrip('=')


def decrypt(input_string: str):
    padding_needed = 4 - (len(input_string) % 4)
    if padding_needed:
        input_string += '=' * padding_needed
    input_bytes = base64.urlsafe_b64decode(input_string)
    result = xor(input_bytes)
    return result.decode()


def xor(input_bytes):
    return bytes([input_bytes[i] ^ key_bytes[i % len(key_bytes)] for i in range(len(input_bytes))])


def urlsafe_base64(input_string: str) -> str:
    input_bytes = input_string.encode("utf-8")
    base64_bytes = base64.urlsafe_b64encode(input_bytes)
    base64_string = base64_bytes.decode("utf-8")
    return base64_string


def urlsafe_base64_decode(base64_string: str) -> str:
    padding = '=' * (-len(base64_string) % 4)
    base64_string_padded = base64_string + padding
    base64_bytes = base64_string_padded.encode("utf-8")
    decoded_bytes = base64.urlsafe_b64decode(base64_bytes)
    return decoded_bytes.decode("utf-8")


def extract_and_decode_var(var_name: str, response: str) -> str:
    pattern = rf'var\s+{re.escape(var_name)}\s*=\s*atob\("([^"]+)"\);'
    matches = re.findall(pattern, response)
    if not matches:
        raise ValueError(f"Variable '{var_name}' not found in response")
    b64 = matches[-1]
    return base64.b64decode(b64).decode("utf-8")


def decode_bundle(response_text: str) -> dict:
    candidates = set()
    candidates.update(re.findall(r'JSON\.parse\s*\(\s*atob\s*\(\s*["\']([^"\']{40,})["\']\s*\)\s*\)', response_text))
    candidates.update(re.findall(r'atob\s*\(\s*["\'](eyJ[A-Za-z0-9+/=]{40,})["\']\s*\)', response_text))
    candidates.update(re.findall(r'(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*["\'](eyJ[A-Za-z0-9+/=]{40,})["\']', response_text))
    candidates.update(re.findall(r'["\'](eyJ[A-Za-z0-9+/=]{40,})["\']', response_text))
    candidates.update(re.findall(r'["\']([A-Za-z0-9+/=]{80,})["\']', response_text))

    for candidate in candidates:
        try:
            decoded_candidate = base64.b64decode(candidate).decode("utf-8")
            data = json.loads(decoded_candidate)
            if not all(key in data for key in ['b_ts', 'b_sig', 'b_rnd', 'b_host']):
                continue
            decoded = {}
            for k, v in data.items():
                if isinstance(v, str):
                    try:
                        pad = '=' * (-len(v) % 4)
                        decoded[k] = base64.b64decode(v + pad).decode("utf-8")
                    except Exception:
                        decoded[k] = v
                else:
                    decoded[k] = v
            return decoded
        except Exception:
            continue
    return {}

def b64url_bytes(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)

def custom_aes_decrypt(line: str, b64key: str, seq: int) -> str:
    key = b64url_bytes(b64key)
    sealed = b64url_bytes(line)
    playlist_iv = struct.pack(">QQ", 0, seq & 0xFFFFFFFFFFFFFFFF)
    decryptor = Cipher(algorithms.AES(key), modes.CTR(playlist_iv)).decryptor()
    plaintext = decryptor.update(sealed) + decryptor.finalize()
    return plaintext.decode("utf-8")
