import hashlib

_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GENERATORS = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _convertbits(raw):
    accumulator = 0
    bits = 0
    result = []
    for byte in raw:
        accumulator = accumulator << 8 | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            result.append(accumulator >> bits & 31)
    if bits:
        result.append(accumulator << (5 - bits) & 31)
    return result


def _polymod(values):
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = (checksum & 0x1FFFFFF) << 5 ^ value
        for index, generator in enumerate(_GENERATORS):
            if top >> index & 1:
                checksum ^= generator
    return checksum


def synthetic_identity(label="synthetic"):
    hrp = "age-secret-key-"
    data = _convertbits(hashlib.sha256(f"josh-room-test:{label}".encode()).digest())
    checksum = _polymod([*(ord(value) >> 5 for value in hrp), 0, *(ord(value) & 31 for value in hrp), *data, 0, 0, 0, 0, 0, 0]) ^ 1
    check = [(checksum >> (5 * (5 - index))) & 31 for index in range(6)]
    suffix = "1" + "".join(_ALPHABET[value] for value in [*data, *check])
    return "AGE-SECRET-KEY-" + suffix
