import csv
import json
import pendulum
import singer
from singer import metadata
from tap_marketo.client import ExportFailed
from singer import bookmarks

# We can request up to 30 days worth of activities per export.
MAX_EXPORT_DAYS = 30

BASE_ACTIVITY_FIELDS = [
    "marketoGUID",
    "leadId",
    "activityDate",
    "activityTypeId",
]

ACTIVITY_FIELDS = BASE_ACTIVITY_FIELDS + [
    "primaryAttributeValue",
    "primaryAttributeValueId",
    "attributes",
]

NO_ASSET_MSG = "No assets found for the given search criteria."
NO_CORONA_WARNING = (
    "Your account does not have Corona support enabled. Without Corona, each sync of "
    "the Leads table requires a full export which can lead to lower data freshness. "
    "Please contact Marketo to request Corona support be added to your account."
)
DEFAULT_CHUNK_SIZE = 5 * (2 ** 20)  # 5MB chunk size
ATTRIBUTION_WINDOW_DAYS = 1


class NonRectangularCsvRow(Exception):
    def __init__(self, headers, row):
        super(Exception, self).__init__("Non-rectangular CSV row detected.")
        self.headers = headers
        self.row = row

def download_chunk(client, stream_type, export_id, start, chunk_size):
    endpoint = client.get_bulk_endpoint(stream_type, "file", export_id)
    headers = {"Range": "bytes={}-{}".format(start, start + chunk_size)}
    resp = client._request("GET", endpoint, headers=headers)
    return resp.content

def get_export_size(client, stream_type, export_id):
    endpoint = client.get_bulk_endpoint(stream_type, "status", export_id)
    resp = client._request("GET", endpoint)
    return resp.json()["result"][0]["fileSize"]

def decode_utf8_chunk(chunk):
    # Since we're getting a chunk of utf-8 bytes we need to fall back one char
    # at a time until we can successfully decode. Since the longest utf-8 char
    # is 4 bytes long, if the 4th decode fails then we have an actual unicode
    # decoding error and should let that surface.
    try:
        unicode_chunk = chunk.decode('utf-8')
        return unicode_chunk, b""
    except UnicodeDecodeError:
        try:
            unicode_chunk = chunk[:-1].decode('utf-8')
            return unicode_chunk, chunk[-1:]
        except UnicodeDecodeError:
            try:
                unicode_chunk = chunk[:-2].decode('utf-8')
                return unicode_chunk, chunk[-2:]
            except UnicodeDecodeError:
                # If this fails then there's really an encoding error
                unicode_chunk = chunk[:-3].decode('utf-8')
                return unicode_chunk, chunk[-3:]

def gen_export_rows(client, stream_type, export_id, chunk_size=DEFAULT_CHUNK_SIZE):
    # To download the CSV we need to know the export size so we can chunk the
    # file download so that the connection doesn't snap in the middle. Each
    # chunk has the possibility of ending with a partial unicode character, so
    # the chunk is decoded using the `decode_utf8_chunk` function which returns
    # the decoded chunk plus any bytes from a partial unicode character that
    # remain. Once we have a unicode chunk, we have to split the lines keeping
    # the newlines that appear inside of a line.
    #
    # The unicode chunk likely ends with a partial CSV line, so we save that as
    # a partial row for the next cycle. We yield that row only after the entire
    # file has been downloaded.

    export_size = get_export_size(client, stream_type, export_id)
    start_byte = 0
    leftover_bytes = b""
    leftover_row = None
    leftover_chars = ""
    headers = None

    while start_byte < export_size:
        # Download the chunk and append it to the leftover bytes
        byte_chunk = download_utf8_chunk(client, stream_type, export_id, start_byte, chunk_size)
        byte_chunk = leftover_bytes + byte_chunk

        # Decode bytes and retain bytes leftover from a partial utf8 char
        unicode_chunk, leftover_bytes = decode_chunk(byte_chunk)

        # Append the decoded unicode string to the leftover chars
        unicode_chunk = leftover_chars + unicode_chunk

        # Split decoded unicode string keeping newlines
        lines = unicode_chunk.splitlines(keepends=True)

        # Parse the unicode string as CSV keeping in mind quoted newlines
        reader = csv.reader(lines, delimiter=',', quotechar='"')
        reader_rows = list(reader)

        # On first loop we have no headers, so pop off the header row
        if headers is None:
            headers = reader_rows.pop(0)

        # Keep the last line for the next chunk
        leftover_row = reader_rows.pop(-1)
        leftover_chars = ",".join(leftover_row)

        # Yield the rows as dicts making sure to check that the number of
        # fields match the number of headers
        for row in reader_rows:
            if len(row) != len(headers):
                raise NonRectangularCsvRow(headers, row)

            yield dict(zip(headers, line))

        # Increment start_byte and continue
        start_byte += chunk_size

    # Check the last row's shape and yield it as a dict if it's rectangular
    if len(leftover_row) != len(headers):
        raise NonRectangularCsvRow(headers, leftover_row)

    yield dict(zip(headers, leftover_row))

def format_value(value, schema):
    if not isinstance(schema["type"], list):
        field_type = [schema["type"]]
    else:
        field_type = schema["type"]

    if value in [None, "", 'null']:
        return None
    elif schema.get("format") == "date-time":
        return pendulum.parse(value).isoformat()
    elif "integer" in field_type:
        return int(value)
    elif "string" in field_type:
        return str(value)
    elif "number" in field_type:
        return float(value)
    elif "boolean" in field_type:
        if isinstance(value, bool):
            return value
        return value.lower() == "true"

    return value

def format_values(stream, row):
    rtn = {}
    for field, schema in stream["schema"]["properties"].items():
        if not schema.get("selected") and not (schema.get("inclusion") == "automatic"):
            continue
        rtn[field] = format_value(row.get(field), schema)
    return rtn

def flatten_activity(row, stream):
    # Start with the base fields
    rtn = {field: row[field] for field in BASE_ACTIVITY_FIELDS}

    # Add the primary attribute name
    # This name is the human readable name/description of the
    # pimaryAttribute
    mdata = metadata.to_map(stream['metadata'])
    pan_field = metadata.get(mdata, (), 'marketo.primary-attribute-name')
    if pan_field:
        rtn['primary_attribute_name'] = pan_field
        rtn['primary_attribute_value'] = row['primaryAttributeValue']
        rtn['primary_attribute_value_id'] = row['primaryAttributeValueId']

    # Now flatten the attrs json to it's selected columns
    if "attributes" in row:
        attrs = json.loads(row["attributes"])
        for key, value in attrs.items():
            key = key.lower().replace(" ", "_")
            rtn[key] = value

    return rtn

def get_or_create_export_for_leads(client, state, stream, fields, start_date):
    export_id = bookmarks.get_bookmark(state, stream["tap_stream_id"], "export_id")
    export_end = bookmarks.get_bookmark(state, stream["tap_stream_id"], "export_end")

    if export_id is None:
        if client.use_corona:
            query_field = "updatedAt"
        else:
            query_field = "createdAt"

        export_end = start_date.add(days=MAX_EXPORT_DAYS)
        if export_end >= pendulum.utcnow():
            export_end = pendulum.utcnow()

        export_end = export_end.replace(microsecond=0)

        query = {query_field: {"startAt": start_date.isoformat(), "endAt": export_end.isoformat()}}
        # Create the new export and store the id and end date in state.
        # Does not start the export (must POST to the "enqueue" endpoint).
        export_id = client.create_export("leads", fields, query)
        update_state_with_export_info(state, stream, export_id=export_id, export_end=export_end.isoformat())

    else:
        export_end = pendulum.parse(export_end)

    return export_id, export_end

def sync_leads(client, state, stream):
    # http://developers.marketo.com/rest-api/bulk-extract/bulk-lead-extract/
    singer.write_schema(stream["tap_stream_id"], stream["schema"], stream["key_properties"])
    record_count = 0
    replication_key = stream.get("replication_key")
    tap_stream_id = stream.get("tap_stream_id")
    tap_job_start_time = pendulum.utcnow()

    fields = [f for f, s in stream["schema"]["properties"].items() if s.get("selected") or (s.get("inclusion") == "automatic")]

    og_bookmark_value = pendulum.parse(bookmarks.get_bookmark(state, tap_stream_id, replication_key))
    bookmark_date = og_bookmark_value.subtract(days=ATTRIBUTION_WINDOW_DAYS)

    stop = pendulum.parse("2017-06-17T00:00:00+00:00")

    while bookmark_date < stop:
        export_id, export_end = get_or_create_export_for_leads(client, state, stream, fields, bookmark_date)

        try:
            client.wait_for_export("leads", export_id)
        except ExportFailed as ex:
            update_state_with_export_info(state, stream)
            singer.log_critical("Export job failure. Status was %s", ex)

        try:
            for row in gen_export_rows(client, "leads", export_id):
                record = format_values(stream, row)
                record_updated_at = record.get('updatedAt', None)
                if client.use_corona or record_updated_at == 'null' or pendulum.parse(record_updated_at) >= og_bookmark_value:
                    singer.write_record(stream["tap_stream_id"], record)
                    record_count += 1
        except Exception as e:
            singer.log_info("Exception while writing leads record, removing export information from state")
            update_state_with_export_info(state, stream)
            raise e

        if client.use_corona:
            state = update_state_with_export_info(state, stream, bookmark=export_end.isoformat(),
                                                  export_id=None, export_end=None)
        else:
            state = update_state_with_export_info(state, stream,
                                                  export_id=None, export_end=None)

        bookmark_date = export_end

    state = update_state_with_export_info(state, stream, bookmark=bookmark_date.isoformat(),
                                          export_id=None, export_end=None)


    return state, record_count

def get_or_create_export_for_activities(client, state, stream):
    export_id = bookmarks.get_bookmark(state, stream["tap_stream_id"], "export_id")

    if not export_id:
        # The activity id is in the top-most breadcrumb of the metatdata
        # Activity ids correspond to activity type id in Marketo.
        # We need the activity type id to build the query.
        activity_metadata = metadata.to_map(stream["metadata"])
        activity_type_id = metadata.get(activity_metadata, (), 'marketo.activity-id')
        singer.log_info("activity id for stream %s is %d", stream["tap_stream_id"], activity_type_id)

        # Activities must be queried by `createdAt` even though
        # that is not a real field. `createdAt` proxies `activityDate`.
        # The activity type id must also be included in the query. The
        # largest date range that can be used for activities is 30 days.
        start_date = bookmarks.get_bookmark(state, stream["tap_stream_id"], stream["replication_key"])
        start_pen = pendulum.parse(start_date)
        end_pen = start_pen.add(days=MAX_EXPORT_DAYS)
        if end_pen >= pendulum.utcnow():
            end_pen = pendulum.utcnow()
        end_pen = end_pen.replace(microsecond=0)
        end_date = end_pen.isoformat()

        query = {"createdAt": {"startAt": start_pen.isoformat(), "endAt": end_date},
                 "activityTypeIds": [activity_type_id]}
        singer.log_info("scheduling export for stream \"%s\" with query: %s", stream["tap_stream_id"], query)
        # Create the new export and store the id and end date in state.
        # Does not start the export (must POST to the "enqueue" endpoint).
        export_id = client.create_export("activities", ACTIVITY_FIELDS, query)
        update_state_with_export_info(state, stream, export_id=export_id, export_end=end_date)

    return export_id


def update_state_with_export_info(state, stream, bookmark=None, export_id=None, export_end=None):
    state = bookmarks.write_bookmark(state, stream["tap_stream_id"], "export_id", export_id)
    state = bookmarks.write_bookmark(state, stream["tap_stream_id"], "export_end", export_end)
    if bookmark:
        state = bookmarks.write_bookmark(state, stream["tap_stream_id"], stream["replication_key"], bookmark)

    singer.write_state(state)
    return state

def convert_line(stream, headers, line):
    row = dict(zip(headers, line))
    row = flatten_activity(row, stream)
    return format_values(stream, row)


def handle_record(state, stream, record):
    start_date = bookmarks.get_bookmark(state, stream["tap_stream_id"], stream["replication_key"])
    if record[stream["replication_key"]] < start_date:
        return 0

    singer.write_record(stream["tap_stream_id"], record)
    return 1


def wait_for_activity_export(client, state, stream, export_id):
    try:
        client.wait_for_export("activities", export_id)
    except ExportFailed:
        update_state_with_export_info(state, stream)
        raise


def sync_activities(client, state, stream):
    # http://developers.marketo.com/rest-api/bulk-extract/bulk-activity-extract/
    singer.write_schema(stream["tap_stream_id"], stream["schema"], stream["key_properties"])
    start_date = bookmarks.get_bookmark(state, stream["tap_stream_id"], stream["replication_key"])
    start_pen = pendulum.parse(start_date)
    job_started = pendulum.utcnow()
    record_count = 0

    while start_pen < job_started:
        export_id = get_or_create_export_for_activities(client, state, stream)

        # If the export fails while running, clear the export information
        # from state so a new export can be run next sync.
        try:
            wait_for_activity_export(client, state, stream, export_id)
        except ExportFailed as ex:
            update_state_with_export_info(state, stream)
            singer.log_critical("Export job failure. Status was %s", ex)

        try:
            for row in gen_export_rows(client, "activities", export_id):
                record = format_values(stream, row)
                singer.write_record(stream["tap_stream_id"], record)
                record_count += 1
        except Exception as e:
            singer.log_info("Exception while writing activity \"%s\" record, removing export information from state", stream["tap_stream_id"])
            update_state_with_export_info(state, stream)
            raise e

        # The new start date is the end of the previous export. Update
        # the bookmark to the end date and continue with the next export.
        start_date = bookmarks.get_bookmark(state, stream["tap_stream_id"], "export_end")
        update_state_with_export_info(state, stream, bookmark=start_date)
        start_pen = pendulum.parse(start_date)

    return state, record_count


def sync_programs(client, state, stream):
    # http://developers.marketo.com/rest-api/assets/programs/#by_date_range
    #
    # Programs are queryable via their updatedAt time but require and
    # end date as well. As there is no max time range for the query,
    # query from the bookmark value until current.
    #
    # The Programs endpoint uses offsets with a return limit of 200
    # per page. If requesting past the final program, an error message
    # is returned to indicate that the endpoint has been fully synced.
    singer.write_schema("programs", stream["schema"], stream["key_properties"])
    start_date = bookmarks.get_bookmark(state, "programs", "updatedAt")
    end_date = pendulum.utcnow().isoformat()
    params = {
        "maxReturn": 200,
        "offset": 0,
        "earliestUpdatedAt": start_date,
        "latestUpdatedAt": end_date,
    }
    endpoint = "rest/asset/v1/programs.json"

    record_count = 0
    while True:
        data = client.request("GET", endpoint, endpoint_name="programs", params=params)

        # If the no asset message is in the warnings, we have exhausted
        # the search results and can end the sync.
        if NO_ASSET_MSG in data["warnings"]:
            break

        # Each row just needs the values formatted. If the record is
        # newer than the original start date, stream the record.
        for row in data["result"]:
            record = format_values(stream, row)
            if record["updatedAt"] >= start_date:
                record_count += 1
                singer.write_record("programs", record)

        # Increment the offset by the return limit for the next query.
        params["offset"] += params["maxReturn"]

    # Now that we've finished every page we can update the bookmark to
    # the end of the query.
    state = bookmarks.write_bookmark(state, "programs", "updatedAt", end_date)
    singer.write_state(state)
    return state, record_count


def sync_paginated(client, state, stream):
    # http://developers.marketo.com/rest-api/endpoint-reference/lead-database-endpoint-reference/#!/Campaigns/getCampaignsUsingGET
    # http://developers.marketo.com/rest-api/endpoint-reference/lead-database-endpoint-reference/#!/Static_Lists/getListsUsingGET
    #
    # Campaigns and Static Lists are paginated with a max return of 300
    # items per page. There are no filters that can be used to only
    # return updated records.
    singer.write_schema(stream["tap_stream_id"], stream["schema"], stream["key_properties"])
    start_date = bookmarks.get_bookmark(state, stream["tap_stream_id"], stream["replication_key"])
    params = {"batchSize": 300}
    endpoint = "rest/v1/{}.json".format(stream["tap_stream_id"])

    # Paginated requests use paging tokens for retrieving the next page
    # of results. These tokens are stored in the state for resuming
    # syncs. If a paging token exists in state, use it.
    next_page_token = bookmarks.get_bookmark(state, stream["tap_stream_id"], "next_page_token")
    if next_page_token:
        params["nextPageToken"] = next_page_token

    # Keep querying pages of data until no next page token.
    record_count = 0
    job_started = pendulum.utcnow().isoformat()
    while True:
        data = client.request("GET", endpoint, endpoint_name=stream["tap_stream_id"], params=params)

        # Each row just needs the values formatted. If the record is
        # newer than the original start date, stream the record. Finally,
        # update the bookmark if newer than the existing bookmark.
        for row in data["result"]:
            record = format_values(stream, row)
            if record[stream["replication_key"]] >= start_date:
                record_count += 1
                singer.write_record(stream["tap_stream_id"], record)

        # No next page, results are exhausted.
        if "nextPageToken" not in data:
            break

        # Store the next page token in state and continue.
        params["nextPageToken"] = data["nextPageToken"]
        state = bookmarks.write_bookmark(state, stream["tap_stream_id"], "next_page_token", data["nextPageToken"])
        singer.write_state(state)

    # Once all results are exhausted, unset the next page token bookmark
    # so the subsequent sync starts from the beginning.
    state = bookmarks.write_bookmark(state, stream["tap_stream_id"], "next_page_token", None)
    state = bookmarks.write_bookmark(state, stream["tap_stream_id"], stream["replication_key"], job_started)
    singer.write_state(state)
    return state, record_count


def sync_activity_types(client, state, stream):
    # http://developers.marketo.com/rest-api/lead-database/activities/#describe
    #
    # Activity types aren't even paginated. Grab all the results in one
    # request, format the values, and output them.
    singer.write_schema("activity_types", stream["schema"], stream["key_properties"])
    endpoint = "rest/v1/activities/types.json"
    data = client.request("GET", endpoint, endpoint_name="activity_types")
    record_count = 0
    for row in data["result"]:
        record = format_values(stream, row)
        record_count += 1
        singer.write_record("activity_types", record)

    return state, record_count


def sync(client, catalog, state):
    starting_stream = bookmarks.get_currently_syncing(state)
    if starting_stream:
        singer.log_info("Resuming sync from %s", starting_stream)
    else:
        singer.log_info("Starting sync")

    for stream in catalog["streams"]:
        # Skip unselected streams.
        if not stream["schema"].get("selected"):
            singer.log_info("%s: not selected", stream["tap_stream_id"])
            continue

        # Skip streams that have already be synced when resuming.
        if starting_stream and stream["tap_stream_id"] != starting_stream:
            singer.log_info("%s: already synced", stream["tap_stream_id"])
            continue

        singer.log_info("%s: starting sync", stream["tap_stream_id"])

        # Now that we've started, there's no more "starting stream". Set
        # the current stream to resume on next run.
        starting_stream = None
        state = bookmarks.set_currently_syncing(state, stream["tap_stream_id"])
        singer.write_state(state)

        # Sync stream based on type.
        if stream["tap_stream_id"] == "activity_types":
            state, record_count = sync_activity_types(client, state, stream)
        elif stream["tap_stream_id"] == "leads":
            state, record_count = sync_leads(client, state, stream)
        elif stream["tap_stream_id"].startswith("activities_"):
            state, record_count = sync_activities(client, state, stream)
        elif stream["tap_stream_id"] in ["campaigns", "lists"]:
            state, record_count = sync_paginated(client, state, stream)
        elif stream["tap_stream_id"] == "programs":
            state, record_count = sync_programs(client, state, stream)
        else:
            raise Exception("Stream %s not implemented" % stream["tap_stream_id"])

        # Emit metric for record count.
        counter = singer.metrics.record_counter(stream["tap_stream_id"])
        counter.value = record_count
        counter._pop()  # pylint: disable=protected-access

        # Unset current stream.
        state = bookmarks.set_currently_syncing(state, None)
        singer.write_state(state)
        singer.log_info("%s: finished sync", stream["tap_stream_id"])

    # If Corona is not supported, log a warning near the end of the tap
    # log with instructions on how to get Corona supported.
    singer.log_info("Performing final Corona check")
    if not client.use_corona:
        singer.log_info("Finished sync")
        singer.log_warning(NO_CORONA_WARNING)
    else:
        singer.log_info("Finished sync")
