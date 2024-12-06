import logging
from csv import DictWriter, DictReader
import os.path
from json import dumps
from typing import List, Mapping, Optional

from progress.bar import Bar

from .card_client import CardClient
from .consts import DEFAULT_FIELDS, EXTENDED_FIELDS
from .identifiers import id_to_str, identifier_names_to_schemes
from .people_client import PeopleClient

LOG = logging.getLogger(__name__)

def export_cards(
    configuration: Mapping, card_client: CardClient, people_client: PeopleClient, *, silent=False
):
    """
    Allows for the export of cards to a csv location based on the configuration passed in.

    For exports based on institutions, groups, or crsids Lookup is queried first to get the name
    and crsids of the applicable members, and these crsids are then used to query the Card API.

    A heuristic is used to display a progress bar tracking the progress of queries to the
    Card API.
    """

    filter_params = configuration.get("filter", configuration.get("params", {})).items()
    queries = configuration.get("queries", [])

    output_config = configuration.get("output", {})
    export_fields = output_config.get("fields")
    deduplicate = output_config.get("deduplicate", False)
    export_location = output_config.get("file", "export.csv")

    if not queries:
        raise ValueError("config.queries must be non-empty")

    # Keep a list of card ids (the unique uuid of a card) in order to deduplicate and
    # to report the number of cards exported
    card_ids_seen: List[str] = []

    # Attempt to import existing export file to use as cache in order to reduce 
    # load on server when requesting card details through card_client.get_card_detail
    cacheddata = {}
    if os.path.isfile(export_location):
        with open(export_location, 'r', encoding="utf-8") as data:
            for line in DictReader(data):
                if line.get('id') is not None:
                    cacheddata[line['id']] = line

    with open(export_location, "w", newline="", encoding="utf-8") as export_file:
        # Allow lazy-init of dict writer giving us the ability to create the writer
        # with the headers coming from the keys of the card response
        writer: Optional[DictWriter] = None

        # Loop through each query - run the query using people_client and then
        # write the cards returned for those people returned using card_client
        for query in queries:
            (ids_to_people, id_scheme) = people_client.get_people_info_for_query(query)

            # Person identifiers will usually be crsids, but may also be another id of any
            # of the schemes documented in `identifiers.py`.
            person_identifiers = list(ids_to_people.keys())
            person_identifiers_seen: List[str] = []

            progress = (
                Bar("   Fetching cards", max=len(person_identifiers))
                if not silent and person_identifiers
                else None
            )

            for card in card_client.cards_for_identifiers(person_identifiers):
                if deduplicate and card["id"] in card_ids_seen:
                    continue

                if not all([card[key] == value for key, value in filter_params]):
                    continue

                person_id_for_card = CardClient.get_identifier_by_scheme(card, id_scheme)

                card_ids_seen.append(card["id"])

                # Update our list of people that we have a card for - this allows us to
                # track progress. We don't know how many cards will be returned but we
                # do know how many person_ids we have queried by
                if person_id_for_card not in person_identifiers_seen:
                    person_identifiers_seen.append(person_id_for_card)

                person_information = (
                    ids_to_people.get(person_id_for_card, {}) if person_id_for_card else {}
                )

                normalized_card = CardClient.normalize_card(card)

                enhanced_card = {**person_information, **normalized_card}

                # cardclientplus allow two new fields 'lastnote' and 'lastnoteAt'
                # If these fields are set in config file, check cache from last 
                # exported file using updatedAt field to see if we need to call 
                # get_card_detail to retrieve latest values. If 'lastnote' / 
                # 'lastnoteAt' fields not set in config file, don't call get_card_detail
                if len(set(EXTENDED_FIELDS).intersection(export_fields)) > 0:
                    enhanced_card['lastnote'] = ''
                    enhanced_card['lastnoteAt'] = ''
                    cardid = normalized_card['id']
                    cacheUpdatedAt = None
                    if cacheddata.get(cardid) is not None:
                        if cacheddata[cardid].get('lastnote') is not None:
                            enhanced_card['lastnote'] = cacheddata[cardid]['lastnote']
                        if cacheddata[cardid].get('lastnoteAt') is not None:
                            enhanced_card['lastnoteAt'] = cacheddata[cardid]['lastnoteAt']
                        if cacheddata[cardid].get('updatedAt') is not None:
                            cacheUpdatedAt = cacheddata[cardid]['updatedAt']

                    if enhanced_card['updatedAt'] != cacheUpdatedAt:
                        detailed_card_record = card_client.get_card_detail(normalized_card['id'])
                        if len(detailed_card_record['notes']) > 0:
                            lastnote = detailed_card_record['notes'][-1]
                            enhanced_card['lastnote'] = lastnote['text']
                            enhanced_card['lastnoteAt'] = lastnote['createdAt']

                if not writer:
                    # Lazy-init the dict writer in order to allow us to set the field names
                    # based on the fields that are returned from the Card API
                    writer = DictWriter(
                        export_file,
                        fieldnames=export_fields or set([*DEFAULT_FIELDS, *EXTENDED_FIELDS, *enhanced_card.keys()]),
                        extrasaction="ignore",
                    )
                    writer.writeheader()

                writer.writerow(enhanced_card)

                if progress:
                    progress.goto(len(person_identifiers_seen))

            if progress:
                progress.goto(len(person_identifiers))
                progress.finish()

        LOG.info(f"Exported {len(card_ids_seen)} cards to {export_location}")


def print_card_detail(
    card_client: CardClient,
    identifier: str,
    identifier_scheme_name: Optional[str] = None,
    normalize: bool = False,
):
    """
    Method which prints details of a card to stdout in JSON format

    """
    # if an identifier with a scheme has been passed we may have multiple card uuids to fetch
    card_uuids = []

    if identifier_scheme_name:
        identifier_scheme = identifier_names_to_schemes.get(identifier_scheme_name)
        if not identifier_scheme:
            schemes = ", ".join(identifier_names_to_schemes)
            raise ValueError(
                f"{identifier_scheme_name} not a recognized id scheme, must be one of: {schemes}"
            )

        results = list(
            card_client.cards_for_identifiers([id_to_str(identifier, identifier_scheme)])
        )
        if not results:
            raise ValueError(f"No card records for {identifier_scheme_name} {identifier}")

        card_uuids = map(lambda card: card["id"], results)
    else:
        card_uuids = [identifier]

    detailed_card_records = map(lambda uuid: card_client.get_card_detail(uuid), card_uuids)
    if normalize:
        detailed_card_records = map(CardClient.normalize_card, detailed_card_records)

    print(dumps(list(detailed_card_records), indent=4))
