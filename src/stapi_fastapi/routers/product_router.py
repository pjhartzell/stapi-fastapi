from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING, Self

from fastapi import APIRouter, HTTPException, Request, Response, status
from geojson_pydantic.geometries import Geometry
from returns.maybe import Some
from returns.result import Failure, Success

from stapi_fastapi.constants import TYPE_JSON
from stapi_fastapi.exceptions import ConstraintsException
from stapi_fastapi.models.opportunity import (
    OpportunityCollection,
    OpportunityPayload,
)
from stapi_fastapi.models.order import Order, OrderPayload
from stapi_fastapi.models.product import Product
from stapi_fastapi.models.shared import Link
from stapi_fastapi.responses import GeoJSONResponse
from stapi_fastapi.types.json_schema_model import JsonSchemaModel

if TYPE_CHECKING:
    from stapi_fastapi.routers import RootRouter

logger = logging.getLogger(__name__)


class ProductRouter(APIRouter):
    def __init__(
        self,
        product: Product,
        root_router: RootRouter,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.product = product
        self.root_router = root_router

        self.add_api_route(
            path="",
            endpoint=self.get_product,
            name=f"{self.root_router.name}:{self.product.id}:get-product",
            methods=["GET"],
            summary="Retrieve this product",
            tags=["Products"],
        )

        self.add_api_route(
            path="/opportunities",
            endpoint=self.search_opportunities,
            name=f"{self.root_router.name}:{self.product.id}:search-opportunities",
            methods=["POST"],
            response_class=GeoJSONResponse,
            # unknown why mypy can't see the constraints property on Product, ignoring
            response_model=OpportunityCollection[
                Geometry,
                self.product.opportunity_properties,  # type: ignore
            ],
            summary="Search Opportunities for the product",
            tags=["Products"],
        )

        self.add_api_route(
            path="/constraints",
            endpoint=self.get_product_constraints,
            name=f"{self.root_router.name}:{self.product.id}:get-constraints",
            methods=["GET"],
            summary="Get constraints for the product",
            tags=["Products"],
        )

        self.add_api_route(
            path="/order-parameters",
            endpoint=self.get_product_order_parameters,
            name=f"{self.root_router.name}:{self.product.id}:get-order-parameters",
            methods=["GET"],
            summary="Get order parameters for the product",
            tags=["Products"],
        )

        # This wraps `self.create_order` to explicitly parameterize `OrderRequest`
        # for this Product. This must be done programmatically instead of with a type
        # annotation because it's setting the type dynamically instead of statically, and
        # pydantic needs this type annotation when doing object conversion. This cannot be done
        # directly to `self.create_order` because doing it there changes
        # the annotation on every `ProductRouter` instance's `create_order`, not just
        # this one's.
        async def _create_order(
            payload: OrderPayload,
            request: Request,
            response: Response,
        ) -> Order:
            return await self.create_order(payload, request, response)

        _create_order.__annotations__["payload"] = OrderPayload[
            self.product.order_parameters  # type: ignore
        ]

        self.add_api_route(
            path="/orders",
            endpoint=_create_order,
            name=f"{self.root_router.name}:{self.product.id}:create-order",
            methods=["POST"],
            response_class=GeoJSONResponse,
            status_code=status.HTTP_201_CREATED,
            summary="Create an order for the product",
            tags=["Products"],
        )

    def get_product(self, request: Request) -> Product:
        return self.product.with_links(
            links=[
                Link(
                    href=str(
                        request.url_for(
                            f"{self.root_router.name}:{self.product.id}:get-product",
                        ),
                    ),
                    rel="self",
                    type=TYPE_JSON,
                ),
                Link(
                    href=str(
                        request.url_for(
                            f"{self.root_router.name}:{self.product.id}:get-constraints",
                        ),
                    ),
                    rel="constraints",
                    type=TYPE_JSON,
                ),
                Link(
                    href=str(
                        request.url_for(
                            f"{self.root_router.name}:{self.product.id}:get-order-parameters",
                        ),
                    ),
                    rel="order-parameters",
                    type=TYPE_JSON,
                ),
                Link(
                    href=str(
                        request.url_for(
                            f"{self.root_router.name}:{self.product.id}:search-opportunities",
                        ),
                    ),
                    rel="opportunities",
                    type=TYPE_JSON,
                ),
                Link(
                    href=str(
                        request.url_for(
                            f"{self.root_router.name}:{self.product.id}:create-order",
                        ),
                    ),
                    rel="create-order",
                    type=TYPE_JSON,
                ),
            ],
        )

    async def search_opportunities(
        self,
        search: OpportunityPayload,
        request: Request,
    ) -> OpportunityCollection:
        """
        Explore the opportunities available for a particular set of constraints
        """
        links: list[Link] = []
        match await self.product._search_opportunities(
            self,
            search,
            search.next,
            search.limit,
            request,
        ):
            case Success((features, Some(pagination_token))):
                links.append(self.order_link(request, search))
                links.append(self.pagination_link(request, search, pagination_token))
            case Success((features, Nothing)):  # noqa: F841
                links.append(self.order_link(request, search))
            case Failure(e) if isinstance(e, ConstraintsException):
                raise e
            case Failure(e):
                logger.error(
                    "An error occurred while searching opportunities: %s",
                    traceback.format_exception(e),
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Error searching opportunities",
                )
            case x:
                raise AssertionError(f"Expected code to be unreachable {x}")
        return OpportunityCollection(features=features, links=links)

    def get_product_constraints(self: Self) -> JsonSchemaModel:
        """
        Return supported constraints of a specific product
        """
        return self.product.constraints

    def get_product_order_parameters(self: Self) -> JsonSchemaModel:
        """
        Return supported constraints of a specific product
        """
        return self.product.order_parameters

    async def create_order(
        self, payload: OrderPayload, request: Request, response: Response
    ) -> Order:
        """
        Create a new order.
        """
        match await self.product.create_order(
            self,
            payload,
            request,
        ):
            case Success(order):
                order.links.extend(self.root_router.order_links(order, request))
                location = str(self.root_router.generate_order_href(request, order.id))
                response.headers["Location"] = location
                return order
            case Failure(e) if isinstance(e, ConstraintsException):
                raise e
            case Failure(e):
                logger.error(
                    "An error occurred while creating order: %s",
                    traceback.format_exception(e),
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Error creating order",
                )
            case x:
                raise AssertionError(f"Expected code to be unreachable {x}")

    def order_link(self, request: Request, opp_req: OpportunityPayload):
        return Link(
            href=str(
                request.url_for(
                    f"{self.root_router.name}:{self.product.id}:create-order",
                ),
            ),
            rel="create-order",
            type=TYPE_JSON,
            method="POST",
            body=opp_req.search_body(),
        )

    def pagination_link(
        self, request: Request, opp_req: OpportunityPayload, pagination_token: str
    ):
        body = opp_req.body()
        body["next"] = pagination_token
        return Link(
            href=str(request.url),
            rel="next",
            type=TYPE_JSON,
            method="POST",
            body=body,
        )
