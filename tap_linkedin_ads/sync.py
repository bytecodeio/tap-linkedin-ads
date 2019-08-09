from datetime import datetime
import singer
from singer import metrics, metadata, Transformer, utils
from tap_linkedin_ads.transform import transform_json

LOGGER = singer.get_logger()


def write_schema(catalog, stream_name):
    stream = catalog.get_stream(stream_name)
    schema = stream.schema.to_dict()
    try:
        singer.write_schema(stream_name, schema, stream.key_properties)
    except OSError as err:
        LOGGER.info('OS Error writing schema for: {}'.format(stream_name))
        raise err


def write_record(stream_name, record, time_extracted):
    try:
        singer.write_record(stream_name, record, time_extracted=time_extracted)
    except OSError as err:
        LOGGER.info('OS Error writing record for: {}'.format(stream_name))
        LOGGER.info('record: {}'.format(record))
        raise err


def process_records(catalog, #pylint: disable=too-many-branches
                    stream_name,
                    records,
                    time_extracted,
                    bookmark_field=None,
                    bookmark_type=None,
                    max_bookmark_value=None,
                    last_datetime=None,
                    last_integer=None,
                    parent=None,
                    parent_id=None):
    stream = catalog.get_stream(stream_name)
    schema = stream.schema.to_dict()
    stream_metadata = metadata.to_map(stream.metadata)

    with metrics.record_counter(stream_name) as counter:
        for record in records:
            # If child object, add parent_id to record
            if parent_id and parent:
                record[parent + '_id'] = parent_id

            # Reset max_bookmark_value to new value if higher
            if bookmark_field and (bookmark_field in record):
                if (max_bookmark_value is None) or \
                    (record[bookmark_field] > max_bookmark_value):
                    max_bookmark_value = record[bookmark_field]

            # Transform record for Singer.io
            with Transformer() as transformer:
                record = transformer.transform(record,
                                               schema,
                                               stream_metadata)

                if bookmark_field and (bookmark_field in record):
                    if bookmark_type == 'integer':
                        # Keep only records whose bookmark is after the last_integer
                        if record[bookmark_field] >= last_integer:
                            write_record(stream_name, record, time_extracted=time_extracted)
                            counter.increment()
                    elif bookmark_type == 'datetime':
                        last_dttm = transformer._transform_datetime(last_datetime)
                        bookmark_dttm = transformer._transform_datetime(record[bookmark_field])
                        # Keep only records whose bookmark is after the last_datetime
                        if bookmark_dttm >= last_dttm:
                            write_record(stream_name, record, time_extracted=time_extracted)
                            counter.increment()
                else:
                    write_record(stream_name, record, time_extracted=time_extracted)
                    counter.increment()

        return max_bookmark_value, counter.value


def get_bookmark(state, stream, default):
    if (state is None) or ('bookmarks' not in state):
        return default
    return (
        state
        .get('bookmarks', {})
        .get(stream, default)
    )


def write_bookmark(state, stream, value):
    if 'bookmarks' not in state:
        state['bookmarks'] = {}
    state['bookmarks'][stream] = value
    singer.write_state(state)


# Sync a specific parent or child endpoint.
def sync_endpoint(client, #pylint: disable=too-many-branches
                  catalog,
                  state,
                  start_date,
                  stream_name,
                  path,
                  data_key,
                  static_params,
                  bookmark_path,
                  bookmark_query_field,
                  bookmark_field,
                  bookmark_type=None,
                  id_fields=None,
                  parent=None,
                  parent_id=None):
    bookmark_path = bookmark_path + [bookmark_field]

    # Get the latest bookmark for the stream and set the last_integer/datetime
    last_datetime = None
    last_integer = None
    max_bookmark_value = None
    if bookmark_type == 'integer':
        last_integer = get_bookmark(state, stream_name, 0)
        max_bookmark_value = last_integer
    else:
        last_datetime = get_bookmark(state, stream_name, start_date)
        max_bookmark_value = last_datetime

    write_schema(catalog, stream_name)

    # Pagination reference:
    # https://docs.microsoft.com/en-us/linkedin/shared/api-guide/concepts/pagination?context=linkedin/marketing/context
    # Each page has a "start" (offset value) and a "count" (batch size, number of records)
    # Increase the "start" by the "count" for each batch.
    # Continue until the "start" exceeds the total_records.
    start = 0 # Starting offset value for each batch API call
    count = 100 # Batch size; Number of records per API call
    total_records = count # Initialize total; set to actual total on first API call

    while start <= total_records:
        params = {
            'start': start,
            'count': count,
            **static_params # adds in endpoint specific, sort, filter params
        }

        if bookmark_query_field:
            if bookmark_type == 'datetime':
                params[bookmark_query_field] = last_datetime
            elif bookmark_type == 'integer':
                params[bookmark_query_field] = last_integer

        LOGGER.info('{} - Sync start {}'.format(
            stream_name,
            'since: {}, '.format(last_datetime) if bookmark_query_field else ''))

        # Squash params to query-string params
        querystring = '&'.join(['%s=%s' % (key, value) for (key, value) in params.items()])
        LOGGER.info('URL for {}: https://api.linkedin.com/v2/{}?{}'\
            .format(stream_name, path, querystring))

        # Get data, API request
        data = client.get(
            path,
            params=querystring,
            endpoint=stream_name)
        # time_extracted: datetime when the data was extracted from the API
        time_extracted = utils.now()

        # Transform data with transform_json from transform.py
        #  This function converts unix datetimes, de-nests audit fields,
        #  tranforms URNs to IDs, tranforms/abstracts variably named fields,
        #  converts camelCase to snake_case for fieldname keys.
        # For the Linkedin Ads API, 'elements' is always the root data_key for records.
        # The data_key identifies the collection of records below the <root> element
        # LOGGER.info('data = {}'.format(data)) # TESTING, comment out
        ids = [] # Initialize the ids list
        transformed_data = [] # initialize the record list
        if data_key in data:
            transformed_data = transform_json(data, stream_name)[data_key]
        LOGGER.info('transformed_data = {}'.format(transformed_data))  # TESTING, comment out
        if not transformed_data or transformed_data is None:
            break # No data results

        # Stores parent object ids for children (return ids at end of function)
        for record in transformed_data:
            rec_ids = {}
            for id_field in id_fields:
                rec_ids[id_field] = record.get(id_field)
                ids.append(rec_ids)

        # Process records and get the max_bookmark_value and record_count for the set of records
        max_bookmark_value, record_count = process_records(
            catalog=catalog,
            stream_name=stream_name,
            records=transformed_data,
            time_extracted=time_extracted,
            bookmark_field=bookmark_field,
            bookmark_type=bookmark_type,
            max_bookmark_value=max_bookmark_value,
            last_datetime=last_datetime,
            last_integer=last_integer,
            parent=parent,
            parent_id=parent_id)

        # Update the state with the max_bookmark_value for the stream
        if bookmark_field:
            write_bookmark(state,
                           stream_name,
                           max_bookmark_value)

        # set total_records for pagination
        if 'total' in data['paging']:
            total_records = data['paging']['total']
        else:
            total_records = record_count  # OR should this be offset + record_count?

        # to_rec: to record; ending record for the batch
        to_rec = start + count
        if to_rec > total_records:
            to_rec = total_records

        LOGGER.info('{} - Synced - {} to {} of total records: {}'.format(
            stream_name,
            start,
            to_rec,
            total_records))
        # Pagination: increment the offset by the count (batch-size)
        start = start + count

    # Return the list of ids to the stream, in case this is a parent stream with children.
    return ids


# Sync a specific stream and its children streams.
def sync_stream(client, #pylint: disable=too-many-branches
                catalog,
                state,
                start_date,
                id_bag,
                stream_name,
                endpoint_config,
                bookmark_path=None,
                id_path=None,
                parent_id=None):
    if not bookmark_path:
        bookmark_path = [stream_name]
    if not id_path:
        id_path = []

    path = endpoint_config.get('path').format(*id_path)
    stream_ids = sync_endpoint(
        client=client,
        catalog=catalog,
        state=state,
        start_date=start_date,
        stream_name=stream_name,
        path=path,
        data_key=endpoint_config.get('data_path', stream_name),
        static_params=endpoint_config.get('params', {}),
        bookmark_path=bookmark_path,
        bookmark_query_field=endpoint_config.get('bookmark_query_field'),
        bookmark_field=endpoint_config.get('bookmark_field'),
        bookmark_type=endpoint_config.get('bookmark_type'),
        id_fields=endpoint_config.get('id_fields'),
        parent=endpoint_config.get('parent'),
        parent_id=parent_id)

    # Stores IDs for parent streams, to be loop through for children
    if endpoint_config.get('store_ids'):
        id_bag[stream_name] = stream_ids

    children = endpoint_config.get('children')
    if children:
        # Loop through parent IDs for each child element
        for child_stream_name, child_endpoint_config in children.items():
            should_stream, last_stream_child = should_sync_stream(
                get_selected_streams(catalog),
                None,
                child_stream_name)
            if should_stream:
                LOGGER.info('START Syncing: {}'.format(child_stream_name))
                for _ids in stream_ids:
                    _id = _ids['id']
                    parent_id = _ids['id']

                    # Add children filter params based on parent IDs
                    if stream_name == 'accounts':
                        account = 'urn:li:sponsoredAccount:{}'.format(_id)
                        if _ids['reference_organization_id'] is None:
                            owner = client.organization
                        else:
                            owner = 'urn:li:organization:{}'.format(_ids\
                                ['reference_organization_id'])
                        if child_stream_name == 'video_ads':
                            child_endpoint_config['params']['account'] = account
                            child_endpoint_config['params']['owner'] = owner
                    elif stream_name == 'campaigns':
                        campaign = 'urn:li:sponsoredCampaign:{}'.format(_id)
                        if child_stream_name == 'creatives':
                            child_endpoint_config['params']['search.campaign.values[0]'] = campaign
                        elif child_stream_name == 'ad_analytics_by_campaign':
                            child_endpoint_config['params']['campaigns[0]'] = campaign
                    elif stream_name == 'creatives':
                        creative = 'urn:li:sponsoredCreative:{}'.format(_id)
                        if child_stream_name == 'ad_analytics_by_creative':
                            child_endpoint_config['params']['creatives[0]'] = creative

                    sync_stream(
                        client=client,
                        catalog=catalog,
                        state=state,
                        start_date=start_date,
                        id_bag=id_bag,
                        stream_name=child_stream_name,
                        endpoint_config=child_endpoint_config,
                        bookmark_path=bookmark_path, # + [_id, child_stream_name],
                        id_path=id_path, # + [_id],
                        parent_id=_id)
                LOGGER.info('FINISHED Syncing: {}'.format(child_stream_name))


# Review catalog and make a list of selected streams
def get_selected_streams(catalog):
    selected_streams = set()
    for stream in catalog.streams:
        mdata = metadata.to_map(stream.metadata)
        root_metadata = mdata.get(())
        if root_metadata and root_metadata.get('selected') is True:
            selected_streams.add(stream.tap_stream_id)
    return list(selected_streams)


# Currently syncing sets the stream currently being delivered in the state.
# If the integration is interrupted, this state property is used to identify
#  the starting point to continue from.
# Reference: https://github.com/singer-io/singer-python/blob/master/singer/bookmarks.py#L41-L46
def update_currently_syncing(state, stream_name):
    if (stream_name is None) and ('currently_syncing' in state):
        del state['currently_syncing']
    else:
        singer.set_currently_syncing(state, stream_name)
    singer.write_state(state)


# Review last_stream (last currently syncing stream), if any,
#  and continue where it left off in the selected streams.
# Or begin from the beginning, if no last_stream, and sync
#  all selected steams.
# Returns should_sync_stream (true/false) and last_stream.
def should_sync_stream(selected_streams, last_stream, stream_name):
    if last_stream == stream_name or last_stream is None:
        if last_stream is not None:
            last_stream = None
        if stream_name in selected_streams:
            return True, last_stream
    return False, last_stream


def sync(client, config, catalog, state):
    if 'start_date' in config:
        start_date = config['start_date']

    # Get datetimes for endpoint parameters
    now = datetime.now()
    analytics_campaign_dt_str = get_bookmark(state, 'ad_analytics_by_campaign', start_date)
    analytics_campaign_dt = datetime.strptime(analytics_campaign_dt_str, "%Y-%m-%dT%H:%M:%SZ")
    analytics_creative_dt_str = get_bookmark(state, 'ad_analytics_by_creative', start_date)
    analytics_creative_dt = datetime.strptime(analytics_creative_dt_str, "%Y-%m-%dT%H:%M:%SZ")

    selected_streams = get_selected_streams(catalog)
    LOGGER.info('selected_streams: {}'.format(selected_streams))

    if not selected_streams:
        return

    # last_stream = Previous currently synced stream, if the load was interrupted
    last_stream = singer.get_currently_syncing(state)
    LOGGER.info('last/currently syncing stream: {}'.format(last_stream))
    id_bag = {}


    # endpoints: API URL endpoints to be called
    # properties:
    #   <root node>: Plural stream name for the endpoint
    #   path: API endpoint relative path, when added to the base URL, creates the full path
    #   account_filter: Method for Account filtering. Each uses a different query pattern/parameter:
    #        search_id_values_param, search_account_values_param, accounts_param
    #   params: Query, sort, and other endpoint specific parameters
    #   data_path: JSON element containing the records for the endpoint
    #   bookmark_query_field: Typically a date-time field used for filtering the query
    #   bookmark_field: Replication key field, typically a date-time, used for filtering the results
    #        and setting the state
    #   bookmark_type: Data type for bookmark, integer or datetime
    #   store_ids: Used for parents to create an id_bag collection of ids for children endpoints
    #   id_fields: Primary key (and other IDs) from the Parent stored when store_ids is true.
    #   children: A collection of child endpoints (where the endpoint path includes the parent id)
    #   parent: On each of the children, the singular stream name for parent element
    #       NOT NEEDED FOR THIS INTEGRATION (The Children all include a reference to the Parent)
    endpoints = {
        'accounts': {
            'path': 'adAccountsV2',
            'account_filter': 'search_id_values_param',
            'params': {
                'q': 'search',
                'sort.field': 'ID',
                'sort.order': 'ASCENDING'
            },
            'data_path': 'elements',
            'bookmark_field': 'last_modified_time',
            'bookmark_type': 'datetime',
            'id_fields': ['id', 'reference_organization_id'],
            'store_ids': True,
            'children': {
                'video_ads': {
                    'path': 'adDirectSponsoredContents',
                    'account_filter': None,
                    'params': {
                        'q': 'account'
                    },
                    'data_path': 'elements',
                    'bookmark_field': 'last_modified_time',
                    'bookmark_type': 'datetime',
                    'id_fields': ['content_reference']
                }
            }
        },

        'account_users': {
            'path': 'adAccountUsersV2',
            'account_filter': 'accounts_param',
            'params': {
                'q': 'accounts'
            },
            'data_path': 'elements',
            'bookmark_field': 'last_modified_time',
            'bookmark_type': 'datetime',
            'id_fields': ['account_id', 'user_person_id']
        },

        'campaign_groups': {
            'path': 'adCampaignGroupsV2',
            'account_filter': 'search_account_values_param',
            'params': {
                'q': 'search',
                'sort.field': 'ID',
                'sort.order': 'ASCENDING'
            },
            'data_path': 'elements',
            'bookmark_field': 'last_modified_time',
            'bookmark_type': 'datetime',
            'id_fields': ['id']
        },

        'campaigns': {
            'path': 'adCampaignsV2',
            'account_filter': 'search_account_values_param',
            'params': {
                'q': 'search',
                'sort.field': 'ID',
                'sort.order': 'ASCENDING'
            },
            'data_path': 'elements',
            'bookmark_field': 'last_modified_time',
            'bookmark_type': 'datetime',
            'id_fields': ['id'],
            'children': {
                'ad_analytics_by_campaign': {
                    'path': 'adAnalyticsV2',
                    'account_filter': 'accounts_param',
                    'params': {
                        'q': 'analytics',
                        'pivot': 'CAMPAIGN',
                        'timeGranularity': 'DAILY',
                        'dateRange.start.day': analytics_campaign_dt.day,
                        'dateRange.start.month': analytics_campaign_dt.month,
                        'dateRange.start.year': analytics_campaign_dt.year,
                        'dateRange.end.day': now.day,
                        'dateRange.end.month': now.month,
                        'dateRange.end.year': now.year
                    },
                    'data_path': 'elements',
                    'bookmark_field': 'end_at',
                    'bookmark_type': 'datetime',
                    'id_fields': ['creative_id', 'start_at']
                },
                'creatives': {
                    'path': 'adCreativesV2',
                    'account_filter': None,
                    'params': {
                        'q': 'search',
                        'search.campaign.values[0]': 'urn:li:sponsoredCampaign:{}',
                        'sort.field': 'ID',
                        'sort.order': 'ASCENDING'
                    },
                    'data_path': 'elements',
                    'bookmark_field': 'last_modified_time',
                    'bookmark_type': 'datetime',
                    'id_fields': ['id'],
                    'children': {
                        'ad_analytics_by_creative': {
                            'path': 'adAnalyticsV2',
                            'account_filter': 'accounts_param',
                            'params': {
                                'q': 'analytics',
                                'pivot': 'CREATIVE',
                                'timeGranularity': 'DAILY',
                                'dateRange.start.day': analytics_creative_dt.day,
                                'dateRange.start.month': analytics_creative_dt.month,
                                'dateRange.start.year': analytics_creative_dt.year,
                                'dateRange.end.day': now.day,
                                'dateRange.end.month': now.month,
                                'dateRange.end.year': now.year
                            },
                            'data_path': 'elements',
                            'bookmark_field': 'end_at',
                            'bookmark_type': 'datetime',
                            'id_fields': ['creative_id', 'start_at']
                        }
                    }
                }
            }
        }
    }

    # For each endpoint (above), determine if the stream should be streamed
    #   (based on the catalog and last_stream), then sync those streams.
    for stream_name, endpoint_config in endpoints.items():
        should_stream, last_stream = should_sync_stream(selected_streams,
                                                        last_stream,
                                                        stream_name)
        if should_stream:
            # Add appropriate account_filter query parameters based on account_filter type
            account_filter = endpoint_config.get('account_filter', None)
            if 'accounts' in config and account_filter is not None:
                account_list = config['accounts'].replace(" ", "").split(",")
                for idx, account in enumerate(account_list):
                    if account_filter == 'search_id_values_param':
                        endpoint_config['params']['search.id.values[{}]'.format(idx)] = int(account)
                    elif account_filter == 'search_account_values_param':
                        endpoint_config['params']['search.account.values[{}]'.format(idx)] = \
                            'urn:li:sponsoredAccount:{}'.format(account)
                    elif account_filter == 'accounts_param':
                        endpoint_config['params']['accounts[{}]'.format(idx)] = \
                            'urn:li:sponsoredAccount:{}'.format(account)

            LOGGER.info('START Syncing: {}'.format(stream_name))
            update_currently_syncing(state, stream_name)
            sync_stream(
                client=client,
                catalog=catalog,
                state=state,
                start_date=start_date,
                id_bag=id_bag,
                stream_name=stream_name,
                endpoint_config=endpoint_config)
            update_currently_syncing(state, None)
            LOGGER.info('FINISHED Syncing: {}'.format(stream_name))
