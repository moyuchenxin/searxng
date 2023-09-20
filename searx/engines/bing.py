# SPDX-License-Identifier: AGPL-3.0-or-later
# lint: pylint
"""This is the implementation of the Bing-WEB engine. Some of this
implementations are shared by other engines:

- :ref:`bing images engine`
- :ref:`bing news engine`
- :ref:`bing videos engine`

On the `preference page`_ Bing offers a lot of languages an regions (see section
'Search results languages' and 'Country/region').  However, the abundant choice
does not correspond to reality, where Bing has a full-text indexer only for a
limited number of languages.  By example: you can select a language like Māori
but you never get a result in this language.

What comes a bit closer to the truth are the `search-APIs`_ but they don`t seem
to be completely correct either (if you take a closer look you will find some
inaccuracies there too):

- :py:obj:`searx.engines.bing.bing_traits_url`
- :py:obj:`searx.engines.bing_videos.bing_traits_url`
- :py:obj:`searx.engines.bing_images.bing_traits_url`
- :py:obj:`searx.engines.bing_news.bing_traits_url`

.. _preference page: https://www.bing.com/account/general
.. _search-APIs: https://learn.microsoft.com/en-us/bing/search-apis/

"""
# pylint: disable=too-many-branches, invalid-name

from typing import TYPE_CHECKING
import base64
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse
from lxml import html
import babel
import babel.languages

from searx.utils import eval_xpath, extract_text, eval_xpath_list, eval_xpath_getindex
from searx.locales import language_tag, region_tag
from searx.enginelib.traits import EngineTraits

if TYPE_CHECKING:
    import logging

    logger: logging.Logger

traits: EngineTraits

about = {
    "website": 'https://www.bing.com',
    "wikidata_id": 'Q182496',
    "official_api_documentation": 'https://www.microsoft.com/en-us/bing/apis/bing-web-search-api',
    "use_official_api": False,
    "require_api_key": False,
    "results": 'HTML',
}

# engine dependent config
categories = ['general', 'web']
paging = True
time_range_support = True

base_url = 'https://www.bing.com/search'
"""Bing (Web) search URL"""

bing_traits_url = 'https://learn.microsoft.com/en-us/bing/search-apis/bing-web-search/reference/market-codes'
"""Bing (Web) search API description"""


def _page_offset(pageno):
    return (int(pageno) - 1) * 10 + 1


def set_bing_cookies(params, engine_language, engine_region):
    params['cookies']['_EDGE_CD'] = f'm={engine_region.lower()}&u={engine_language.lower()};'


def request(query, params):
    """Assemble a Bing-Web request."""

    engine_region = traits.get_region(params['searxng_locale'], 'en-us')
    engine_language = traits.get_language(params['searxng_locale'], 'en-us')
    set_bing_cookies(params, engine_language, engine_region)

    query_params = {'q': query, 'first': _page_offset(params.get('pageno', 1))}
    params['url'] = f'{base_url}?{urlencode(query_params)}'

    unix_day = int(time.time() / 86400)
    time_ranges = {'day': '1', 'week': '2', 'month': '3', 'year': f'5_{unix_day-365}_{unix_day}'}
    if params.get('time_range') in time_ranges:
        params['url'] += f'&filters=ex1:"ez{time_ranges[params["time_range"]]}"'

    return params


def response(resp):
    # pylint: disable=too-many-locals

    results = []
    result_len = 0

    dom = html.fromstring(resp.text)

    # parse results again if nothing is found yet

    for result in eval_xpath_list(dom, '//ol[@id="b_results"]/li[contains(@class, "b_algo")]'):

        link = eval_xpath_getindex(result, './/h2/a', 0, None)
        if link is None:
            continue
        url = link.attrib.get('href')
        title = extract_text(link)

        content = eval_xpath(result, '(.//p)[1]')
        for p in content:
            # Make sure that the element is free of <a href> links
            for e in p.xpath('.//a'):
                e.getparent().remove(e)
        content = extract_text(content)

        # get the real URL
        if url.startswith('https://www.bing.com/ck/a?'):
            # get the first value of u parameter
            url_query = urlparse(url).query
            parsed_url_query = parse_qs(url_query)
            param_u = parsed_url_query["u"][0]
            # remove "a1" in front
            encoded_url = param_u[2:]
            # add padding
            encoded_url = encoded_url + '=' * (-len(encoded_url) % 4)
            # decode base64 encoded URL
            url = base64.urlsafe_b64decode(encoded_url).decode()

        # append result
        results.append({'url': url, 'title': title, 'content': content})

    # get number_of_results
    try:
        result_len_container = "".join(eval_xpath(dom, '//span[@class="sb_count"]//text()'))
        if "-" in result_len_container:

            # Remove the part "from-to" for paginated request ...
            result_len_container = result_len_container[result_len_container.find("-") * 2 + 2 :]

        result_len_container = re.sub('[^0-9]', '', result_len_container)

        if len(result_len_container) > 0:
            result_len = int(result_len_container)

    except Exception as e:  # pylint: disable=broad-except
        logger.debug('result error :\n%s', e)

    if result_len and _page_offset(resp.search_params.get("pageno", 0)) > result_len:
        # Avoid reading more results than avalaible.
        # For example, if there is 100 results from some search and we try to get results from 120 to 130,
        # Bing will send back the results from 0 to 10 and no error.
        # If we compare results count with the first parameter of the request we can avoid this "invalid" results.
        return []

    results.append({'number_of_results': result_len})
    return results


def fetch_traits(engine_traits: EngineTraits):
    """Fetch languages and regions from Bing-Web."""

    xpath_market_codes = '//table[1]/tbody/tr/td[3]'
    # xpath_country_codes = '//table[2]/tbody/tr/td[2]'
    xpath_language_codes = '//table[3]/tbody/tr/td[2]'

    _fetch_traits(engine_traits, bing_traits_url, xpath_language_codes, xpath_market_codes)


def _fetch_traits(engine_traits: EngineTraits, url: str, xpath_language_codes: str, xpath_market_codes: str):
    # pylint: disable=too-many-locals,import-outside-toplevel

    from searx.network import get  # see https://github.com/searxng/searxng/issues/762

    # insert alias to map from a language (zh) to a language + script (zh_Hans)
    engine_traits.languages['zh'] = 'zh-hans'

    resp = get(url)

    if not resp.ok:  # type: ignore
        print("ERROR: response from peertube is not OK.")

    dom = html.fromstring(resp.text)  # type: ignore

    map_lang = {'jp': 'ja'}
    for td in eval_xpath(dom, xpath_language_codes):
        eng_lang = td.text

        if eng_lang in ('en-gb', 'pt-br'):
            # language 'en' is already in the list and a language 'en-gb' can't
            # be handled in SearXNG, same with pt-br which is covered by pt-pt.
            continue

        babel_lang = map_lang.get(eng_lang, eng_lang).replace('-', '_')
        try:
            sxng_tag = language_tag(babel.Locale.parse(babel_lang))
        except babel.UnknownLocaleError:
            print("ERROR: language (%s) is unknown by babel" % (eng_lang))
            continue
        conflict = engine_traits.languages.get(sxng_tag)
        if conflict:
            if conflict != eng_lang:
                print("CONFLICT: babel %s --> %s, %s" % (sxng_tag, conflict, eng_lang))
            continue
        engine_traits.languages[sxng_tag] = eng_lang

    map_region = {
        'en-ID': 'id_ID',
        'no-NO': 'nb_NO',
    }

    for td in eval_xpath(dom, xpath_market_codes):
        eng_region = td.text
        babel_region = map_region.get(eng_region, eng_region).replace('-', '_')

        if eng_region == 'en-WW':
            engine_traits.all_locale = eng_region
            continue

        try:
            sxng_tag = region_tag(babel.Locale.parse(babel_region))
        except babel.UnknownLocaleError:
            print("ERROR: region (%s) is unknown by babel" % (eng_region))
            continue
        conflict = engine_traits.regions.get(sxng_tag)
        if conflict:
            if conflict != eng_region:
                print("CONFLICT: babel %s --> %s, %s" % (sxng_tag, conflict, eng_region))
            continue
        engine_traits.regions[sxng_tag] = eng_region
