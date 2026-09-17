"""FREE-IMPORT-AI-PROPOSAL-01 — deterministic single-product URL
classification.  No model calls, ever: the classifier reads schema.org
JSON-LD + og:type captured by the page extractor only.

Contract:
  exactly one Product entity + no multi structure → "single"
  ProductGroup / ItemList / OfferCatalog / >1 Product → "multi"
  anything else (incl. og:type=product alone)        → "ambiguous"
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.url_import import _PageExtractor, classify_product_signals


def _ld(*objs) -> list[str]:
    """Wrap JSON-LD objects as separate script bodies."""
    return [json.dumps(o, ensure_ascii=False) for o in objs]


PRODUCT = {
    "@type": "Product",
    "name": "K1 Watch",
    "url": "https://shop.example/p/k1",
    "offers": {"@type": "Offer", "price": "990"},
}


class TestClassify:
    def test_single_product_is_single(self):
        out = classify_product_signals(_ld(PRODUCT))
        assert out["page_class"] == "single"
        assert out["product_count"] == 1
        assert out["multi_signals"] == []

    def test_product_inside_graph(self):
        out = classify_product_signals(_ld({"@graph": [
            {"@type": "WebPage", "name": "page"},
            {"@type": "Product", "name": "K1"},
        ]}))
        assert out["page_class"] == "single"

    def test_product_inside_main_entity(self):
        out = classify_product_signals(_ld({
            "@type": "WebPage",
            "mainEntity": {"@type": "Product", "name": "K1"},
        }))
        assert out["page_class"] == "single"

    def test_full_url_type_is_product(self):
        out = classify_product_signals(_ld({"@type": "https://schema.org/Product", "name": "K1"}))
        assert out["page_class"] == "single"

    def test_type_list_with_product(self):
        out = classify_product_signals(_ld({"@type": ["Product", "IndividualProduct"], "name": "K1"}))
        assert out["page_class"] == "single"

    def test_two_products_is_multi(self):
        out = classify_product_signals(_ld(PRODUCT, {"@type": "Product", "name": "K2"}))
        assert out["page_class"] == "multi"
        assert out["product_count"] == 2

    def test_product_group_with_variants_is_single(self):
        """ProductGroup = ONE product family — the single-product signal,
        not a multi signal (FREE-INGEST-FACTS-AND-URL-IDENTITY-01)."""
        out = classify_product_signals(_ld({
            "@type": "ProductGroup", "name": "K1 variants",
            "hasVariant": [{"@type": "Product", "name": "v1"}],
        }))
        assert out["page_class"] == "single"
        assert "ProductGroup" not in out["multi_signals"]

    def test_item_list_is_multi(self):
        out = classify_product_signals(_ld({
            "@type": "ItemList",
            "itemListElement": [{"@type": "ListItem", "position": 1}],
        }))
        assert out["page_class"] == "multi"
        assert "ItemList" in out["multi_signals"]

    def test_offer_catalog_is_multi(self):
        out = classify_product_signals(_ld({"@type": "OfferCatalog", "name": "catalog"}))
        assert out["page_class"] == "multi"
        assert "OfferCatalog" in out["multi_signals"]

    def test_single_product_with_item_list_is_multi(self):
        """A real Product + a contradictory list structure → reject (never
        silently accept)."""
        out = classify_product_signals(_ld(PRODUCT, {"@type": "ItemList"}))
        assert out["page_class"] == "multi"

    def test_no_jsonld_is_ambiguous(self):
        out = classify_product_signals([])
        assert out["page_class"] == "ambiguous"

    def test_og_type_alone_is_ambiguous(self):
        """og:type=product is explicitly NOT sufficient confidence — the
        classifier never sees it (extraction captures it for evidence only)."""
        out = classify_product_signals([])
        assert out["page_class"] == "ambiguous"

    def test_unrelated_jsonld_is_ambiguous(self):
        out = classify_product_signals(_ld({"@type": "BreadcrumbList"}, {"@type": "Organization"}))
        assert out["page_class"] == "ambiguous"

    def test_unparseable_jsonld_is_ambiguous(self):
        out = classify_product_signals(["{not json", ""])
        assert out["page_class"] == "ambiguous"


class TestBaseProductIdentity:
    """GAP B — the gate counts distinct BASE-PRODUCT identities, not raw
    Product nodes.  Variants of one base model collapse; independent
    products stay multi; no-identity pages stay safely rejected."""

    def test_same_product_jsonld_duplicated_is_single(self):
        """The same Product emitted twice (two plugins / blocks) is ONE
        identity — a shared product URL/@id collapses the records."""
        dup = dict(PRODUCT)
        dup["@id"] = "https://shop.example/p/k1#product"
        out = classify_product_signals(_ld(PRODUCT, dup))
        assert out["page_class"] == "single"
        assert out["product_count"] == 1

    def test_identical_product_blocks_no_ids_is_single(self):
        """Byte-identical records = the same entity re-emitted."""
        out = classify_product_signals(_ld(
            {"@type": "Product", "name": "K2"},
            {"@type": "Product", "name": "K2"},
        ))
        assert out["page_class"] == "single"
        assert out["product_count"] == 1

    def test_color_variants_shared_product_url_is_single(self):
        """K2 Black / Pink / Blue — variant urls differ only in query →
        same base product."""
        variants = [
            {"@type": "Product", "name": "K2",
             "url": f"https://shop.example/products/k2?variant={v}",
             "color": v}
            for v in ("black", "pink", "blue")
        ]
        out = classify_product_signals(_ld(*variants))
        assert out["page_class"] == "single"
        assert out["product_count"] == 1

    def test_sku_variants_shared_model_is_single(self):
        """Phone X storage/color SKUs — same stable model identifier →
        one product (different SKUs never split a family)."""
        variants = [
            {"@type": "Product", "name": "Phone X", "model": "Phone X",
             "sku": f"PX-{c}-{s}"}
            for c in ("black", "white") for s in ("128", "256")
        ]
        out = classify_product_signals(_ld(*variants))
        assert out["page_class"] == "single"
        assert out["product_count"] == 1

    def test_is_variant_of_collapses_to_parent(self):
        out = classify_product_signals(_ld(
            {"@type": "Product", "name": "K2 Black",
             "isVariantOf": {"@type": "ProductGroup", "@id": "#k2"}},
            {"@type": "Product", "name": "K2 Pink",
             "isVariantOf": {"@type": "ProductGroup", "@id": "#k2"}},
        ))
        assert out["page_class"] == "single"
        assert out["product_count"] == 1

    def test_same_name_different_families_is_multi(self):
        """Codex regression — name alone is NOT identity evidence: two
        products both called "Classic" under different ProductGroups are
        two distinct base products."""
        out = classify_product_signals(_ld(
            {"@type": "Product", "name": "Classic",
             "isVariantOf": {"@type": "ProductGroup", "@id": "#grp-a"}},
            {"@type": "Product", "name": "Classic",
             "isVariantOf": {"@type": "ProductGroup", "@id": "#grp-b"}},
        ))
        assert out["page_class"] == "multi"
        assert out["product_count"] == 2

    def test_same_name_no_shared_identity_is_multi(self):
        """Two products sharing only a display name (different SKUs, no
        common url/@id/group/model) cannot be proven the same entity."""
        out = classify_product_signals(_ld(
            {"@type": "Product", "name": "Classic", "sku": "A-1"},
            {"@type": "Product", "name": "Classic", "sku": "B-2"},
        ))
        assert out["page_class"] == "multi"
        assert out["product_count"] == 2

    def test_product_group_plus_unrelated_product_is_multi(self):
        """A family + an independent product = two base identities."""
        out = classify_product_signals(_ld(
            {"@type": "ProductGroup", "name": "K2",
             "hasVariant": [{"@type": "Product", "name": "K2 Black"}]},
            {"@type": "Product", "name": "Charger", "sku": "CH-1"},
        ))
        assert out["page_class"] == "multi"
        assert out["product_count"] == 2

    def test_independent_products_stay_multi(self):
        """K2 / K3 / K5 — different names AND different product urls."""
        out = classify_product_signals(_ld(*[
            {"@type": "Product", "name": f"K{n}",
             "url": f"https://shop.example/products/k{n}"}
            for n in (2, 3, 5)
        ]))
        assert out["page_class"] == "multi"
        assert out["product_count"] == 3

    def test_anonymous_products_fail_closed(self):
        """Product nodes asserting no identity evidence at all cannot be
        proven the same product → safely treated as distinct."""
        out = classify_product_signals(_ld(
            {"@type": "Product"}, {"@type": "Product"}))
        assert out["page_class"] == "multi"
        assert out["product_count"] == 2


class TestAncillaryListStructures:
    """GAP B follow-up — an ItemList is only a multi signal when it is the
    page's own structure.  A recommendation list alongside a declared
    primary Product must not reject a genuine single-product page."""

    def test_ancillary_itemlist_with_declared_product_is_single(self):
        """WebPage declares the Product as mainEntity; the ItemList is an
        'also bought' block — still a single-product page."""
        out = classify_product_signals(_ld(
            {"@type": "WebPage", "name": "K1 product page",
             "mainEntity": PRODUCT},
            {"@type": "ItemList", "name": "You may also like",
             "itemListElement": [
                 {"@type": "ListItem", "position": 1,
                  "url": "https://shop.example/p/other1"},
                 {"@type": "ListItem", "position": 2,
                  "url": "https://shop.example/p/other2"},
             ]},
        ))
        assert out["page_class"] == "single"
        assert out["product_count"] == 1

    def test_itemlist_as_primary_content_is_multi(self):
        """A category/listing page whose PRIMARY content is an ItemList
        must still be rejected."""
        out = classify_product_signals(_ld({
            "@type": "CollectionPage",
            "mainEntity": {"@type": "ItemList", "name": "All watches",
                           "itemListElement": [
                               {"@type": "ListItem", "position": 1},
                           ]},
        }))
        assert out["page_class"] == "multi"

    def test_undeclared_itemlist_with_product_stays_multi(self):
        """No declared primary entity → Product + ItemList remains
        ambiguous about page purpose → fail closed."""
        out = classify_product_signals(_ld(
            {"@type": "Product", "name": "K1"},
            {"@type": "ItemList"},
        ))
        assert out["page_class"] == "multi"


class TestExtractorCapture:
    def _extract(self, html: str) -> _PageExtractor:
        ex = _PageExtractor()
        ex.feed(html)
        ex.close()
        return ex

    def test_ld_json_captured(self):
        ex = self._extract(
            '<html><head><script type="application/ld+json">'
            '{"@type":"Product","name":"K1"}'
            '</script></head><body><p>spec text ' + 'x' * 200 + '</p></body></html>')
        assert len(ex.jsonld_chunks) == 1
        assert json.loads(ex.jsonld_chunks[0])["@type"] == "Product"

    def test_plain_script_not_captured_and_not_leaked(self):
        ex = self._extract(
            '<html><body><script>var secret = "tracker();";</script>'
            '<p>real body ' + 'x' * 200 + '</p></body></html>')
        assert ex.jsonld_chunks == []
        assert "tracker" not in "".join(ex.text_chunks)

    def test_og_type_captured(self):
        ex = self._extract(
            '<html><head><meta property="og:type" content="product"></head>'
            '<body><p>' + 'x' * 200 + '</p></body></html>')
        assert ex.meta.get("og_type") == "product"

    def test_multiple_ld_json_blocks(self):
        ex = self._extract(
            '<html><head>'
            '<script type="application/ld+json">{"@type":"BreadcrumbList"}</script>'
            '<script type="application/ld+json">{"@type":"Product","name":"K1"}</script>'
            '</head><body><p>' + 'x' * 200 + '</p></body></html>')
        assert len(ex.jsonld_chunks) == 2
        out = classify_product_signals(ex.jsonld_chunks)
        assert out["page_class"] == "single"


class TestOutboundRelationships:
    """Products referenced through OUTBOUND relationship fields are other
    entities — recommendations the page names, never the page's own product
    or its variants.  They must not be counted as base-product identities,
    must not merge, and must not claim variants or declare the primary
    entity.  FREE-INGEST real-page regression: a genuine single-product
    page whose JSON-LD names related/recommended products was rejected as
    multi."""

    _FIXTURE = (Path(__file__).resolve().parent
                / "fixtures" / "url_thaisuperphone_k5.json")

    def test_real_k5_related_products_page_is_single(self):
        """The exact failing page: one Product (K5 watch) + isRelatedTo
        naming 4 DIFFERENT products (Black Shark, KOSPET x2, KIESLECT) —
        previously counted as 5 identities → multi.  Generic contract:
        one base product + outbound recommendations = single."""
        data = json.loads(self._FIXTURE.read_text(encoding="utf-8"))
        out = classify_product_signals(data["jsonld_chunks"])
        assert out["page_class"] == "single", (
            f"real related-products page must classify single, got {out}")
        assert out["product_count"] == 1

    def test_is_related_to_products_are_not_counted(self):
        """Synthetic shape of the same rule — outbound links skipped."""
        out = classify_product_signals(_ld({
            "@type": "Product", "name": "K5",
            "url": "https://shop.example/p/1",
            "isRelatedTo": [
                {"@type": "Product", "name": "Other A",
                 "url": "https://shop.example/p/2"},
                {"@type": "Product", "name": "Other B",
                 "url": "https://shop.example/p/3"},
            ],
        }))
        assert out["product_count"] == 1
        assert out["page_class"] == "single"

    def test_is_similar_to_recommendation_is_single(self):
        out = classify_product_signals(_ld({
            "@type": "Product", "name": "K5",
            "isSimilarTo": {"@type": "Product", "name": "Similar",
                            "url": "https://shop.example/p/9"},
        }))
        assert out["page_class"] == "single"

    def test_independent_sibling_products_still_multi(self):
        """Same products as PARALLEL nodes (not under an outbound field)
        remain independent identities → multi.  Guards against simply
        ignoring nested products anywhere."""
        out = classify_product_signals(_ld(
            {"@type": "Product", "name": "A",
             "isRelatedTo": [{"@type": "Product", "name": "B",
                              "url": "https://shop.example/p/2"}]},
            {"@type": "Product", "name": "C",
             "url": "https://shop.example/p/3"},
        ))
        assert out["product_count"] == 2
        assert out["page_class"] == "multi"

    def test_offer_item_offered_variants_merge_into_product(self):
        """A product's OWN offers selling variant items (distinct SKUs,
        Shopify-style itemOffered shape) are that product's sellable
        variants — they fold into the base identity."""
        out = classify_product_signals(_ld({
            "@type": "Product", "name": "K5",
            "url": "https://shop.example/p/1",
            "offers": [
                {"@type": "Offer", "price": "2990",
                 "itemOffered": {"@type": "Product",
                                 "name": "K5 Black", "sku": "K5-BLK"}},
                {"@type": "Offer", "price": "2990",
                 "itemOffered": {"@type": "Product",
                                 "name": "K5 Pink", "sku": "K5-PNK"}},
            ],
        }))
        assert out["product_count"] == 1
        assert out["page_class"] == "single"

    def test_standalone_offer_item_offered_fails_closed(self):
        """An Offer's itemOffered NOT nested inside a declared Product is
        not claimed as a variant — distinct products stay multi."""
        out = classify_product_signals(_ld(
            {"@type": "Product", "name": "Main",
             "url": "https://shop.example/p/1"},
            {"@type": "Offer", "price": "100",
             "itemOffered": {"@type": "Product", "name": "Second",
                             "url": "https://shop.example/p/2"}},
        ))
        assert out["product_count"] == 2
        assert out["page_class"] == "multi"
