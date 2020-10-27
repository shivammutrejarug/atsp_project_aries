import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from uuid import uuid4
import string
from motor import motor_asyncio
import coloredlogs

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa

from runners.support.agent import DemoAgent, default_genesis_txns
from runners.support.utils import (
    log_json,
    log_msg,
    log_status,
    log_timer,
    prompt,
    prompt_loop,
    require_indy,
)

CRED_PREVIEW_TYPE = "https://didcomm.org/issue-credential/1.0/credential-preview"
SELF_ATTESTED = os.getenv("SELF_ATTESTED")

# Create a logger object.
logger = logging.getLogger(__name__)

coloredlogs.install(level='DEBUG', logger=logger)

TAILS_FILE_COUNT = int(os.getenv("TAILS_FILE_COUNT", 100))


class AcmeAgent(DemoAgent):
    def __init__(self, http_port: int, admin_port: int, **kwargs):
        super().__init__(
            "Acme Agent",
            http_port,
            admin_port,
            prefix="Acme",
            extra_args=["--auto-accept-invites", "--auto-accept-requests"],
            **kwargs,
        )
        self.connection_id = None
        self._connection_ready = asyncio.Future()
        self.cred_state = {}
        # TODO define a dict to hold credential attributes based on
        # the credential_definition_id
        self.cred_attrs = {}
        self.cred_def_id = None
        self.disease_specification = "melanoma"

    async def detect_connection(self):
        await self._connection_ready

    async def connect_mongo(self):
        self.conn = motor_asyncio.AsyncIOMotorClient("mongodb1:27017")
        self.db = self.conn['kafka']
        self.coll = self.db['agg_test']
        return self.coll

    async def get_data_list(self):
        data_list = []
        # test = await self.coll.find_one({}, {"_id": False})
        async for document in self.coll.find({}, {"_id": False}):
            data_list.append(document)
        n = await self.coll.count_documents({})
        logger.info(n)
        return data_list

    @property
    def connection_ready(self):
        return self._connection_ready.done() and self._connection_ready.result()

    async def handle_connections(self, message):
        if message["connection_id"] == self.connection_id:
            if message["state"] == "active" and not self._connection_ready.done():
                self.log("Connected")
                self._connection_ready.set_result(True)

    async def handle_issue_credential(self, message):
        state = message["state"]
        credential_exchange_id = message["credential_exchange_id"]
        prev_state = self.cred_state.get(credential_exchange_id)
        if prev_state == state:
            return  # ignore
        self.cred_state[credential_exchange_id] = state

        self.log(
            "Credential: state =",
            state,
            ", credential_exchange_id =",
            credential_exchange_id,
        )

        if state == "request_received":
            # TODO issue credentials based on the credential_definition_id
            cred_attrs = self.cred_attrs[message["credential_definition_id"]]
            cred_preview = {
                "@type": CRED_PREVIEW_TYPE,
                "attributes": [
                    {"name": n, "value": v} for (n, v) in cred_attrs.items()
                ],
            }
            await self.admin_POST(
                f"/issue-credential/records/{credential_exchange_id}/issue",
                {
                    "comment": f"Issuing credential, exchange {credential_exchange_id}",
                    "credential_preview": cred_preview
                }
            )

    async def handle_present_proof(self, message):
        state = message["state"]
        variables_dict = {}

        presentation_exchange_id = message["presentation_exchange_id"]
        self.log(
            "Presentation: state =",
            state,
            ", presentation_exchange_id =",
            presentation_exchange_id,
        )

        if state == "presentation_received":
            # TODO handle received presentations
            log_status("#27 Process the proof provided by X")
            log_status("#28 Check if proof is valid")
            proof = await self.admin_POST(
                f"/present-proof/records/{presentation_exchange_id}/verify-presentation"
            )
            self.log("Proof = ", proof["verified"])

            # if presentation is a degree schema (proof of education),
            # check values received
            pres_req = message["presentation_request"]
            pres = message["presentation"]
            is_proof_of_education = (
                pres_req["name"] == "Proof of Education"
            )

            if is_proof_of_education:
                log_status("#28.1 Received proof of education, check claims")
                for (referent, attr_spec) in pres_req["requested_attributes"].items():
                    self.log(
                        f"{attr_spec['name']}: "
                        f"{pres['requested_proof']['revealed_attrs'][referent]['raw']}"
                    )
                for id_spec in pres["identifiers"]:
                    # just print out the schema/cred def id's of presented claims
                    self.log(f"schema_id: {id_spec['schema_id']}")
                    self.log(f"cred_def_id {id_spec['cred_def_id']}")
                # TODO placeholder for the next step
                if proof['verified'] == "true":
                    for (referent, attr_spec) in pres_req["requested_attributes"].items():
                        variables_dict[attr_spec['name']] = pres['requested_proof']['revealed_attrs'][referent]['raw']

                    if variables_dict['role_type'] != self.disease_specification:
                        logger.info(f"This institute is only open for {self.disease_specification} specific research data")
                        self.log("This is the collection ", self.coll)
                        return

                    research_data = await self.get_data_list()
                    logger.info(research_data)

                    await issue_access(
                        self, variables_dict['name'], variables_dict['affiliation'], 
                        variables_dict['role'], variables_dict['role_type']
                    )
            else:
                # in case there are any other kinds of proofs received
                self.log("#28.1 Received ", message["presentation_request"]["name"])
            # pass

    async def handle_basicmessages(self, message):
        self.log("Received message:", message["content"])

    async def close_connection(self):
        self.conn.close()


async def issue_access(
    agent, name, 
    affiliation, role, role_type
    ):
    issue_date = datetime.date(datetime.now())
    expiry_date = issue_date + timedelta(days=31)
    # TODO credential offers
    # TODO Replace date with expiry and issue date
    agent.cred_attrs[agent.cred_def_id] = {
        "researcher_id": ''.join(random.choices(string.ascii_uppercase + string.digits, k = 8)),
        "name": name,
        "issue_date": str(issue_date),
        "issue_timestamp": issue_date.strftime("%s"),
        "expiry_date": str(expiry_date),
        "expiry_timestamp": expiry_date.strftime("%s"),
        "affiliation": affiliation,
        "role": role,
        "role_type": role_type
    }
    cred_preview = {
        "@type": CRED_PREVIEW_TYPE,
        "attributes": [
            {"name": n, "value": v}
            for (n, v) in agent.cred_attrs[agent.cred_def_id].items()
        ],
    }
    offer_request = {
        "connection_id": agent.connection_id,
        "cred_def_id": agent.cred_def_id,
        "comment": f"Offer on cred def id {agent.cred_def_id}",
        "credential_preview": cred_preview,
    }
    await agent.admin_POST(
        "/issue-credential/send-offer",
        offer_request
    )
    return

async def handle_credential_json(agent):
    async for details in prompt_loop("Requested Credential details: "):
        if details:
            try:
                json.loads(details)
                return json.loads(details)
            except json.JSONDecodeError as e:
                log_msg("Invalid credential request:", str(e))


async def main(start_port: int,
    no_auto: bool = False,
    revocation: bool = False,
    show_timing: bool = False
    ):

    genesis = await default_genesis_txns()
    if not genesis:
        print("Error retrieving ledger genesis transactions")
        sys.exit(1)

    agent = None

    try:
        log_status("#1 Provision an agent and wallet, get back configuration details")
        agent = AcmeAgent(
            start_port, start_port + 1, genesis_data=genesis, timing=show_timing
        )
        await agent.listen_webhooks(start_port + 2)
        await agent.register_did()
        await agent.connect_mongo()

        with log_timer("Startup duration:"):
            await agent.start_process()
        log_msg("Admin URL is at:", agent.admin_url)
        log_msg("Endpoint URL is at:", agent.endpoint)

        # Create a schema
        log_status("#3 Create a new schema on the ledger")
        with log_timer("Publish schema and cred def duration:"):
            pass
            # TODO define schema
            version = format(
                "%d.%d.%d"
                % (
                    random.randint(1, 101),
                    random.randint(1, 101),
                    random.randint(1, 101),
                )
            )
            (
                schema_id,
                credential_definition_id,
            ) = await agent.register_schema_and_creddef(
                "employee id schema",
                version,
                [
                    "researcher_id", "name", "issue_date", "issue_timestamp",
                    "expiry_date", "expiry_timestamp", "affiliation", "role",
                    "role_type"
                ],
            )
            agent.cred_def_id = credential_definition_id
        with log_timer("Generate invitation duration:"):
            # Generate an invitation
            log_status(
                "#5 Create a connection to alice and print out the invite details"
            )
            connection = await agent.admin_POST("/connections/create-invitation")

        agent.connection_id = connection["connection_id"]
        log_json(connection, label="Invitation response:")
        log_msg("*****************")
        log_msg(json.dumps(connection["invitation"]), label="Invitation:", color=None)
        log_msg("*****************")

        log_msg("Waiting for connection...")
        await agent.detect_connection()

        exchange_tracing = False

        async for option in prompt_loop(
            "(1) Issue Credential, (2) Send Proof Request, "
            + "(3) Send Message (X) Exit? [1/2/3/X] "
        ):
            option = option.strip()
            if option in "xX":
                await agent.close_connection()
                break

            elif option == "1":
                log_status("#13 Enter details to issue credential.")
                cred_details = await handle_credential_json(agent)
                log_status(f"#13 Issue credential offer to {cred_details['name']}")
                issue_date = str(datetime.date(datetime.now()))
                expiry_date = str(issue_date + timedelta(days=31))
                # TODO credential offers
                # TODO Replace date with expiry and issue date
                agent.cred_attrs[credential_definition_id] = {
                    "researcher_id": ''.join(random.choices(string.ascii_uppercase + string.digits, k = 8)),
                    "name": cred_details['name'],
                    "issue_date": issue_date,
                    "issue_timestamp": issue_date.strftime("%s"),
                    "expiry_date": expiry_date,
                    "expiry_timestamp": expiry_date.strftime("%s"),
                    "affiliation": cred_details['affiliation'],
                    "role": cred_details['role'],
                    "role_type": cred_details['role_type']
                }
                cred_preview = {
                    "@type": CRED_PREVIEW_TYPE,
                    "attributes": [
                        {"name": n, "value": v}
                        for (n, v) in agent.cred_attrs[credential_definition_id].items()
                    ],
                }
                offer_request = {
                    "connection_id": agent.connection_id,
                    "cred_def_id": credential_definition_id,
                    "comment": f"Offer on cred def id {credential_definition_id}",
                    "credential_preview": cred_preview,
                }
                await agent.admin_POST(
                    "/issue-credential/send-offer",
                    offer_request
                )

            elif option == "2":
                req_attrs = [
                    {
                        "name": "name",
                        "restrictions": [{"schema_name": "degree schema"}]
                    },
                    {
                        "name": "issue_date",
                        "restrictions": [{"schema_name": "degree schema"}]
                    },
                    {
                        "name": "expiry_date",
                        "restrictions": [{"schema_name": "degree schema"}]
                    },
                    {
                        "name": "degree",
                        "restrictions": [{"schema_name": "degree schema"}]
                    },
                    {
                        "name": "affiliation",
                        "value": "UA" or "UG",
                        "restrictions": [{"schema_name": "degree schema"}]
                    },
                    {
                        "name": "role",
                        "value": "Researcher" or "researcher",
                        "restrictions": [{"schema_name": "degree schema"}]
                    },
                    {
                        "name": "role_type",
                        "value": "Melanoma" or "melanoma",
                        "restrictions": [{"schema_name": "degree schema"}]
                    }
                ]
                req_preds = [
                    # Check if the credential has expired or not.
                    {
                        "name": "expiry_timestamp",
                        "p_type": ">",
                        "p_value": int(datetime.timestamp(datetime.now())),
                        "restrictions": [{"schema_name": "degree schema"}],
                    }
                ]
                indy_proof_request = {
                    "name": "Proof of Education",
                    "version": "1.0",
                    "nonce": str(uuid4().int),
                    "requested_attributes": {
                        f"0_{req_attr['name']}_uuid": req_attr
                        for req_attr in req_attrs
                    },
                    "requested_predicates": {
                        f"0_{req_pred['name']}_GE_uuid": req_pred
                        for req_pred in req_preds
                    }
                }
                proof_request_web_request = {
                    "connection_id": agent.connection_id,
                    "proof_request": indy_proof_request
                }
                # this sends the request to our agent, which forwards it to Alice
                # (based on the connection_id)
                await agent.admin_POST(
                    "/present-proof/send-request",
                    proof_request_web_request
                )
            elif option == "3":
                msg = await prompt("Enter message: ")
                await agent.admin_POST(
                    f"/connections/{agent.connection_id}/send-message", {"content": msg}
                )

        if show_timing:
            timing = await agent.fetch_timing()
            if timing:
                for line in agent.format_timing(timing):
                    log_msg(line)

    finally:
        terminated = True
        try:
            if agent:
                await agent.terminate()
        except Exception:
            logger.exception("Error terminating agent:")
            terminated = False

    await asyncio.sleep(0.1)

    if not terminated:
        os._exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Runs an Acme demo agent.")
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=8040,
        metavar=("<port>"),
        help="Choose the starting port number to listen on",
    )
    parser.add_argument(
        "--timing", action="store_true", help="Enable timing information"
    )
    args = parser.parse_args()

    require_indy()

    try:
        asyncio.get_event_loop().run_until_complete(main(args.port, args.timing))
    except KeyboardInterrupt:
        os._exit(1)
