"""Валидация ограничений до открытия сетевых сокетов."""

import ipaddress
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class LowConfig:
    """Разрешённые параметры приманки; адрес по умолчанию доступен только локально."""

    host: str = "127.0.0.1"
    tcp_port: int = 2222
    http_port: int = 8080
    banner: str = "SSH-2.0-HoneyForge\r\n"
    http_body: str = "Service available\n"
    read_timeout: float = 2.0
    max_input_bytes: int = 4096
    max_connections: int = 32

    def __post_init__(self) -> None:
        """Отсекает неоднозначные типы и неограниченные значения конфигурации."""
        if not isinstance(self.host, str):
            raise ValueError("host должен быть строковым IP-адресом")
        try:
            ipaddress.ip_address(self.host)
        except ValueError as error:
            raise ValueError("host должен быть IP-адресом без DNS-разрешения") from error
        for name, value in (("tcp_port", self.tcp_port), ("http_port", self.http_port)):
            self._integer(name, value, 0, 65535)
        if self.tcp_port and self.tcp_port == self.http_port:
            raise ValueError("Ненулевые TCP- и HTTP-порты должны различаться")
        self._integer("max_input_bytes", self.max_input_bytes, 1, 65536)
        self._integer("max_connections", self.max_connections, 1, 1024)
        if (
            isinstance(self.read_timeout, bool)
            or not isinstance(self.read_timeout, (int, float))
            or not math.isfinite(self.read_timeout)
            or not 0 < self.read_timeout <= 300
        ):
            raise ValueError("read_timeout должен быть конечным числом от 0 до 300 секунд")
        if not isinstance(self.banner, str):
            raise ValueError("banner должен быть строкой ASCII")
        content = self.banner.removesuffix("\r\n")
        # В сеть баннер всегда уходит с CRLF, а RFC 4253 ограничивает такую строку
        # 255 символами вместе с CRLF: поэтому считается длина с терминатором.
        if (
            not content
            or len(content) + 2 > 255
            or any(not 32 <= ord(character) <= 126 for character in content)
        ):
            raise ValueError("banner допускает ASCII без управляющих символов и завершающий CRLF")
        if not isinstance(self.http_body, str):
            raise ValueError("http_body должен быть строкой")
        try:
            body_bytes = self.http_body.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("http_body должен содержать корректный Unicode") from error
        if len(body_bytes) > 65536:
            raise ValueError("http_body превышает 65536 байт UTF-8")

    @staticmethod
    def _integer(name: str, value: int, minimum: int, maximum: int) -> None:
        """Не принимает bool как число, даже несмотря на наследование от int."""
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} должен быть целым от {minimum} до {maximum}")
