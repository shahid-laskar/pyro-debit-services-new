from Crypto.Cipher import DES
from Crypto.Util.Padding import pad
import base64

# Same key and IV from F_DECRYPT
key = bytes.fromhex('5459353441424358')                          # 8 bytes for DES
iv  = bytes.fromhex('3532414233323B5E')                          # DES uses only first 8 bytes of IV

def encrypt_mpin(mpin: str) -> str:
    cipher = DES.new(key, DES.MODE_CBC, iv)
    padded = pad(mpin.encode('utf-8'), DES.block_size)           # PKCS5 padding
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode('utf-8')

# Test
mpin = "147258"
encrypted = encrypt_mpin(mpin)
print(f"MPIN      : {mpin}")
print(f"Encrypted : {encrypted}")