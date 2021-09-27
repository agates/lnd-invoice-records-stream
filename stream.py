import asyncio
import base64
import binascii
import json
from logging import error
import os
from functools import partial
from pathlib import Path
from typing import Set

import grpc.aio

import lightning_pb2 as ln
import lightning_pb2_grpc as lnrpc


TLV_DECODERS = {
    7629169: partial(bytes.decode, encoding="UTF-8"),
    7629171: partial(bytes.decode, encoding="UTF-8"),
    133773310: partial(bytes.decode, encoding="UTF-8"),
}

LND_CONNECTION_STRINGS = ["127.0.0.1", "umbrel.local"]


class LNDNodeNotFound(Exception):
    pass

def decode_tlv_record(key: int, value: bytes):
    try:
        return TLV_DECODERS[key](value)
    except KeyError:
        # Unknown TLV record type
        return base64.b64encode(value).decode("UTF-8")


def invoice_custom_records(invoice: ln.Invoice, custom_record_keys: Set[int] = None):
    if custom_record_keys:
        return (
            {"key": key, "value": decode_tlv_record(key, value)}
            for htlc in invoice.htlcs
            for key, value in htlc.custom_records.items()
            if key in custom_record_keys
        )
    else:
        return (
            {"key": key, "value": decode_tlv_record(key, value)}
            for htlc in invoice.htlcs
            for key, value in htlc.custom_records.items()
        )


async def find_lnd_node(
    creds: grpc.ChannelCredentials, macaroon: str
) -> lnrpc.LightningStub:
    """First look on local host then look on list of alternates for
    a lightning LND conneciton"""

    for con_str in LND_CONNECTION_STRINGS:
        async with grpc.aio.secure_channel(f"{con_str}:10009", creds) as channel:
            stub = lnrpc.LightningStub(channel)
            request = ln.GetInfoRequest()
            try:
                get_info = await stub.GetInfo(
                    request=request, metadata=[("macaroon", macaroon)]
                )
                print(f"Successful connection to: {con_str}")
                return con_str
            except Exception as ex:
                print(f"{con_str=}")
                print(f"{ex}")
                pass
    raise LNDNodeNotFound("LND Node not found")

async def output_custom_records(
    creds: grpc.ChannelCredentials,
    macaroon: str,
    custom_record_keys: Set[int] = None,
    add_index=0,
):

    con_str = await find_lnd_node(creds=creds, macaroon=macaroon)
    async with grpc.aio.secure_channel(f"{con_str}:10009", creds) as channel:
        stub = lnrpc.LightningStub(channel)
        request = ln.InvoiceSubscription(add_index=add_index)
        invoice_subscription = stub.SubscribeInvoices(
            request, metadata=[("macaroon", macaroon)]
        )

        async for invoice in invoice_subscription:
            try:
                if invoice.is_keysend:
                    for record in invoice_custom_records(invoice, custom_record_keys):
                        print(json.dumps(record))
            except Exception as e:
                # Unknown error
                pass


def startup():
    os.environ["GRPC_SSL_CIPHER_SUITES"] = "HIGH+ECDSA"
    # os.environ["http_proxy"] = "http://127.0.0.1:8118"

    # Add TLV records to this set if you only want some record types
    # custom_record_keys = {7629169}
    custom_record_keys = set()

    # Only get invoices after this index
    # 0 will only stream new indices
    add_index = int(os.getenv("INVOICE_ADD_INDEX", 0))

    credential_path = os.getenv("LND_CRED_PATH", None)

    if credential_path is None:
        credential_path = Path.home().joinpath(".lnd")
        macaroon_filepath = str(
            credential_path.joinpath(
                "data/chain/bitcoin/mainnet/admin.macaroon"
            ).absolute()
        )
    else:
        credential_path = Path(credential_path)
        macaroon_filepath = str(credential_path.joinpath("admin.macaroon").absolute())

    cert_filepath = str(credential_path.joinpath("tls.cert").absolute())

    with open(macaroon_filepath, "rb") as f:
        macaroon_bytes = f.read()
        macaroon = binascii.hexlify(macaroon_bytes).decode()

    cert = open(cert_filepath, "rb").read()
    ssl_creds = grpc.ssl_channel_credentials(cert)

    future = output_custom_records(
        ssl_creds,
        macaroon,
        custom_record_keys,
        add_index=add_index,
    )

    asyncio.ensure_future(future)

    loop = asyncio.get_event_loop()

    loop.run_forever()


if __name__ == "__main__":
    startup()
