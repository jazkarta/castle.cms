from castle.cms.constants import CRAWLED_SITE_ES_DOC_TYPE
from castle.cms.utils import get_public_url
from collective.elasticsearch.es import ElasticSearchCatalog
from collective.elasticsearch.interfaces import IQueryAssembler
from DateTime import DateTime
from elasticsearch import TransportError
from plone import api
from plone.registry.interfaces import IRegistry
from Products.CMFCore.utils import _getAuthenticatedUser
from Products.Five import BrowserView
from urlparse import urljoin
from urlparse import urlparse
from zope.component import getMultiAdapter
from zope.component import getUtility

import datetime
import json
import Missing


def custom_json_handler(obj):
    if obj == Missing.Value:
        return None
    if type(obj) in (datetime.datetime, datetime.date):
        return obj.isoformat()
    if type(obj) == DateTime:
        return obj.ISO8601()
    return obj


def _one(val):
    if val and type(val) == list and len(val) == 1:
        val = val[0]
    return val


class Search(BrowserView):

    @property
    def options(self):
        search_types = [{
            'id': 'images',
            'label': 'Images',
            'query': {
                'portal_type': 'Image'
            }
        }, {
            'id': 'page',
            'label': 'Page',
            'query': {
                'portal_type': ['Document', 'Folder']
            }
        }]

        ptypes = api.portal.get_tool('portal_types')
        for type_id in ptypes.objectIds():
            if type_id in ('Link', 'Document', 'Folder'):
                continue
            _type = ptypes[type_id]
            if not _type.global_allow:
                continue
            search_types.append({
                'id': type_id.lower(),
                'label': _type.title,
                'query': {
                    'portal_type': type_id
                }
            })
        search_types.extend([{
            'id': 'video',
            'label': 'Video',
            'query': {
                'portal_type': 'Video'
            }
        }, {
            'id': 'audio',
            'label': 'Audio',
            'query': {
                'portal_type': 'Audio'
            }
        }])

        additional_sites = []
        es = ElasticSearchCatalog(api.portal.get_tool('portal_catalog'))
        if es.enabled:
            query = {
                "size": 0,
                "aggregations": {
                    "totals": {
                        "terms": {
                            "field": "domain"
                        }
                    }
                }
            }
            try:
                result = es.connection.search(
                    index=es.index_name,
                    doc_type=CRAWLED_SITE_ES_DOC_TYPE,
                    body=query)
                for res in result['aggregations']['totals']['buckets']:
                    site_name = res.get('key')
                    if '.' not in site_name or 'amazon' in site_name:
                        continue
                    additional_sites.append(site_name)
            except TransportError:
                return []

        parsed = urlparse(get_public_url())

        return json.dumps({
            'searchTypes': search_types,
            'additionalSites': [s for s in sorted(additional_sites)],
            'currentSiteLabel': parsed.netloc
        })

    @property
    def search_url(self):
        if api.user.is_anonymous():
            try:
                url = api.portal.get_registry_record('castle.searchurl')
                if url:
                    return url
            except:
                pass
        return '%s/@@searchajax' % (
            self.context.absolute_url()
        )


_search_attributes = [
    'Title',
    'Description',
    'Subject',
    'contentType',
    'created',
    'modified',
    'effective',
    'hasImage',
    'is_folderish',
    'portal_type',
    'review_state',
    'url'
]
_valid_params = [
    'SearchableText',
    'portal_type',
    'Subject'
]


class SearchAjax(BrowserView):

    def __call__(self):
        self.catalog = api.portal.get_tool('portal_catalog')
        self.request.response.setHeader('Content-type', 'application/json')

        query = {}
        for name in _valid_params:
            if self.request.form.get(name):
                query[name] = self.request.form[name]
            elif self.request.form.get(name + '[]'):
                query[name] = self.request.form[name + '[]']

        try:
            page_size = int(self.request.form.get('pageSize'))
        except:
            page_size = 20
        page_size = min(page_size, 50)
        try:
            page = int(self.request.form.get('page'))
        except:
            page = 1
        catalog = api.portal.get_tool('portal_catalog')
        es = ElasticSearchCatalog(catalog)
        if es.enabled:
            return self.get_es_results(page, page_size, query)
        else:
            return self.get_results(page, page_size, query)

    def get_results(self, page, page_size, query):
        # regular plone search
        site_path = '/'.join(self.context.getPhysicalPath())
        start = (page - 1) * page_size
        end = start + page_size
        catalog = api.portal.get_tool('portal_catalog')
        raw_results = catalog(**query)
        items = []

        registry = getUtility(IRegistry)
        view_types = registry.get('plone.types_use_view_action_in_listings', [])

        for brain in raw_results[start:end]:
            attrs = {}
            for key in _search_attributes:
                attrs[key] = getattr(brain, key, None)
            url = base_url = brain.getURL()
            if brain.portal_type in view_types:
                url += '/view'
            attrs.update({
                'path': brain.getPath()[len(site_path):],
                'base_url': base_url,
                'url': url
            })
            items.append(attrs)

        return json.dumps({
            'count': len(raw_results),
            'results': items,
            'page': page,
            'suggestions': []
        }, default=custom_json_handler)

    def get_es_results(self, page, page_size, query):
        start = (page - 1) * page_size
        site_path = '/'.join(self.context.getPhysicalPath())
        base_site_url = self.context.absolute_url()

        results = suggestions = []
        count = 0
        if len(query) > 0:
            results = self.search_es(query, start, page_size)
            count = results['hits']['total']
            try:
                suggestions = results['suggest']['SearchableText'][0]['options']
            except:
                suggestions = []
            results = results['hits']['hits']

        registry = getUtility(IRegistry)
        view_types = registry.get('plone.types_use_view_action_in_listings', [])

        items = []
        for res in results:
            fields = res['fields']

            attrs = {}
            for key in _search_attributes:
                val = fields.get(key)
                attrs[key] = _one(val)

            if 'url' not in fields:
                path = fields.get('path.path', [''])
                path = path[0]
                path = path[len(site_path):]
                url = base_url = urljoin(base_site_url + '/', path.lstrip('/'))
                if attrs.get('portal_type') in view_types:
                    url += '/view'
            else:
                url = base_url = _one(fields['url'])
                parsed = urlparse(url)
                path = parsed.path

            attrs.update({
                'review_state': 'published',
                'score': res['_score'],
                'path': path,
                'base_url': base_url,
                'url': url
            })

            items.append(attrs)

        return json.dumps({
            'count': count,
            'results': items,
            'page': page,
            'suggestions': suggestions
        }, default=custom_json_handler)

    def search_es(self, query, start, size):
        user = _getAuthenticatedUser(self.catalog)
        query['allowedRolesAndUsers'] = self.catalog._listAllowedRolesAndUsers(user)

        es = ElasticSearchCatalog(self.catalog)
        qassembler = getMultiAdapter((self.request, es), IQueryAssembler)
        dquery, sort = qassembler.normalize(query)
        equery = qassembler(dquery)

        doc_type = es.doc_type
        if 'searchSite' in self.request.form:
            doc_type = CRAWLED_SITE_ES_DOC_TYPE
            equery = {
                'filtered': {
                    'filter': {
                        "term": {
                            "domain": self.request.form['searchSite']
                        }
                    },
                    'query': equery['function_score']['query']['filtered']['query']
                }
            }

        query = {
            'query': equery,
            "suggest": {
                "SearchableText": {
                    "text": query.get('SearchableText', ''),
                    "term": {
                        "field": "SearchableText"
                    }
                }
            }
        }

        query_params = {
            'from_': start,
            'size': size,
            'fields': ','.join(_search_attributes) + ',path.path',
        }

        return es.connection.search(index=es.index_name,
                                    doc_type=doc_type,
                                    body=query,
                                    **query_params)
