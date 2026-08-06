The files under `/app/input` define six independent field-season simulations with different grids, boundary topology, crop development, observation calendars, water quality, pump budgets, and management bands. `manifest.json` has the exact schema `{"schema_version": 1, "campaigns": [{"campaign_id": string, "directory": absolute-path}, ...]}` and bytewise campaign order. Each campaign directory contains `job_ticket.json`, `field_boundary.geojson`, `soil_map_units.geojson`, `soil_horizons.csv`, `vegetation_index.csv`, `weather.csv`, `initial_depletion.csv`, `initial_salinity.csv`, and `irrigation_events.geojson`. Dates are ISO 8601 civil dates with inclusive simulation endpoints. Coordinates use the metre-based projected CRS in the ticket.

The ticket has exact keys `campaign_id`, `crs`, `grid`, `simulation`, `crop`, `irrigation`, `salinity`, `pump`, and `management_zone_edges_mm`. `grid` contains `origin_x`, `origin_y`, `cell_width_m`, `cell_height_m`, `row_direction`, and `column_direction`; directions are `north_to_south` and `west_to_east`. `crop` contains `root_depth_curve`, `depletion_fraction`, `kc_slope`, `kc_intercept`, `kc_min`, and `kc_max`. `irrigation` contains `efficiency` and `max_application_mm`. `salinity` contains `rainfall_ec_ds_m`, `crop_threshold_ec_ds_m`, `yield_slope_per_ds_m`, `minimum_stress_coefficient`, `leaching_efficiency`, `new_root_zone_ec_ds_m`, `minimum_solution_depth_mm`, and `leaching_requirement_mm_per_ds_m`. `pump` contains `volume_budget_m3`, `water_deficit_priority_weight`, `salinity_priority_weight`, and `stress_history_priority_weight`.

For zero-based row r and column c, the grid cell spans `[origin_x+c*w, origin_x+(c+1)*w]` in x and `[origin_y-(r+1)*h, origin_y-r*h]` in y. Enumerate cells meeting the field envelope. A cell is an analysis unit exactly when its polygon intersection with the field has positive area. Use that intersection as its geometry, `r{row:04d}c{column:04d}` as its ID, and clipped area divided by field area as `area_fraction`. Fields may be Polygon or MultiPolygon and contain holes. Do not snap, buffer, simplify, reproject, or use centroid inclusion.

`soil_map_units.geojson` is a FeatureCollection whose polygon features have only `map_unit_id`. They form a non-overlapping partition covering the field. `soil_horizons.csv` has exact header `map_unit_id,top_cm,bottom_cm,theta_fc,theta_wp`; intervals are top-inclusive and bottom-exclusive. The strictly increasing `crop.root_depth_curve` contains exact-key objects `{"date": date, "root_depth_m": number}`. Linearly interpolate root depth over civil-day ordinals and clamp beyond the endpoints. For each map unit and day, TAW in millimetres is the sum over horizons of `10 * (theta_fc-theta_wp) * reached_thickness_cm`. Area-weight map-unit TAW by their true intersection areas with the clipped analysis unit. The table covers every specified root depth.

`initial_depletion.csv` has exact header `unit_id,depletion_fraction`; initial absolute depletion is that fraction times start-day TAW. `initial_salinity.csv` has exact header `unit_id,initial_ec_ds_m`. `vegetation_index.csv` has exact header `unit_id,date,vi` and at least two increasing observations per unit, possibly outside the simulation. Linearly interpolate VI over civil-day ordinals and clamp to endpoint observations. Daily `Kc = clip(kc_slope*VI + kc_intercept, kc_min, kc_max)`.

`weather.csv` has exact header `date,eto_mm,effective_precipitation_mm` and one row per simulation date. `irrigation_events.geojson` contains Polygon or MultiPolygon features with exact properties `event_id`, `date`, `gross_depth_mm`, and `water_ec_ds_m`. For each unit/event, covered gross depth is `gross_depth_mm * intersection_area / unit_area`. Multiply it by irrigation efficiency for effective depth. Sum effective depths from all same-day events, including overlaps. Irrigation salt input is the sum of each event's effective depth times its own `water_ec_ds_m`; do not average event EC first.

Advance each unit chronologically in float64 without intermediate rounding. The water depletion state `Dr` is in millimetres; the salt-load state `S` is an EC-depth index in `(dS/m)*mm`. At initialization:

`Dr = initial_depletion_fraction * TAW`

`S = initial_ec_ds_m * max(TAW - Dr, minimum_solution_depth_mm)`

On every day, first calculate that day's TAW and set `Dr = min(Dr, TAW)`. If TAW exceeds the preceding day's TAW, add `(TAW - previous_TAW) * new_root_zone_ec_ds_m` to S; newly explored root-zone storage otherwise enters at field capacity. Then calculate the pre-flux root-zone salinity:

`EC = S / max(TAW - Dr, minimum_solution_depth_mm)`

With `ETc_potential = Kc * ETo`, adjust readily available water as:

`p = clip(crop.depletion_fraction + 0.04*(5 - ETc_potential), 0.1, 0.8)`

`RAW = p * TAW`

`Ks = 1` when `Dr <= RAW`; otherwise `Ks = clip((TAW-Dr)/(TAW-RAW), 0, 1)`.

Salinity stress is:

`Ksal = clip(1 - yield_slope_per_ds_m * max(EC-crop_threshold_ec_ds_m, 0), minimum_stress_coefficient, 1)`

Actual crop use is `ETc = Ks * Ksal * Kc * ETo`. Let P be effective precipitation, I be spatially effective irrigation, and `storage_before = TAW-Dr`. Compute:

`candidate = Dr + ETc - P - I`

`drainage = max(0, -candidate)`

`Dr = clip(candidate, 0, TAW)`

Before leaching, `S_before = S + P*rainfall_ec_ds_m + irrigation_salt_input`. The removed fraction is:

`f = leaching_efficiency * min(1, drainage / max(storage_before + P + I, minimum_solution_depth_mm))`

`leached_salt_index = S_before * f`

`S = max(0, S_before - leached_salt_index)`

Evapotranspiration removes no salt. Accumulate `seasonal_etc_mm`, `seasonal_effective_irrigation_mm`, `seasonal_drainage_mm`, and `seasonal_leached_salt_index`. `stress_days` and `salinity_stress_days` count days for which Ks and Ksal respectively are below one; track `minimum_ks` and `minimum_k_sal`. Report end-date TAW, RAW, Kc, Dr, and `final_ec_ds_m = S / max(TAW-Dr, minimum_solution_depth_mm)`.

First form an unconstrained terminal request. The water-deficit request is zero when final Dr is at most final RAW, otherwise `Dr/irrigation.efficiency`. The gross leaching request is `max(final_EC-crop_threshold_ec_ds_m, 0) * leaching_requirement_mm_per_ds_m / irrigation.efficiency`. `unconstrained_gross_mm` is the lesser of their sum and `irrigation.max_application_mm`.

For N units in one campaign, let `d_i` be unconstrained gross depth, `A_i` clipped area, `D_i` final depletion, `T_i` final TAW, `E_i` final EC, `n_wi` water-stress days, `n_si` salinity-stress days, and Ndays the inclusive calendar length. Its allocation priority is:

`w_i = 1 + water_deficit_priority_weight*(D_i/max(T_i, minimum_solution_depth_mm)) + salinity_priority_weight*max(E_i/crop_threshold_ec_ds_m-1, 0) + stress_history_priority_weight*((n_wi+n_si)/Ndays)`

Allocate all units jointly. Choose depths `x_i` that minimize `sum(A_i*w_i*(d_i-x_i)^2)` subject to `0 <= x_i <= d_i` and `sum(A_i*x_i/1000) <= volume_budget_m3`. If requested volume is within budget, `x_i=d_i`. Otherwise the budget binds and the unique solution is `x_i=max(0, d_i-lambda/(2*w_i))`, where the nonnegative lambda makes `sum(A_i*x_i/1000)` equal the budget. Determine lambda by monotone binary search over `[0, max_i(2*w_i*d_i)]` for exactly 100 iterations, updating the lower endpoint when trial volume exceeds the budget and otherwise the upper endpoint; use the final upper endpoint. Report `recommended_gross_mm=x_i` and `allocation_shortfall_mm=d_i-x_i`.

Classify the allocated recommendation against strictly increasing `management_zone_edges_mm`, starting at zero. For edges `[e0,...,e(n-1)]`, Zi is `[ei,e(i+1))` and the final zone extends to infinity; an edge value belongs to the higher zone.

`/app/output/prescription.geojson` is a FeatureCollection with exact top-level keys `type` and `features`. Each feature has exact keys `type`, `geometry`, and `properties`, type `Feature`, and the clipped unit geometry. Properties have exactly: string `campaign_id`, string `unit_id`, integer `row`, integer `column`, string `zone_id`, finite-number `clipped_area_m2`, `area_fraction`, `taw_mm`, `raw_mm`, `final_dr_mm`, `final_kc`, `final_ec_ds_m`, `minimum_k_sal`, `recommended_gross_mm`, `unconstrained_gross_mm`, `allocation_priority`, `allocation_shortfall_mm`, `seasonal_etc_mm`, `seasonal_effective_irrigation_mm`, `seasonal_drainage_mm`, `seasonal_leached_salt_index`, integer `stress_days`, integer `salinity_stress_days`, and finite-number `minimum_ks`. Order features by bytewise campaign ID, then integer row, then integer column.

`/app/output/units.csv` contains the identical records/order with exact header `campaign_id,unit_id,row,column,zone_id,clipped_area_m2,area_fraction,taw_mm,raw_mm,final_dr_mm,final_kc,final_ec_ds_m,minimum_k_sal,recommended_gross_mm,unconstrained_gross_mm,allocation_priority,allocation_shortfall_mm,seasonal_etc_mm,seasonal_effective_irrigation_mm,seasonal_drainage_mm,seasonal_leached_salt_index,stress_days,salinity_stress_days,minimum_ks`. Integers use base-ten integer text; other numbers use finite non-exponent decimal text.

`/app/output/summary.json` has exact top-level keys `schema_version` and `campaigns`, schema version 1, and bytewise campaign order. Each campaign has exact keys `campaign_id`, `analysis_unit_count`, `field_area_m2`, `irrigated_area_m2`, `area_weighted_mean_depth_mm`, `total_gross_volume_m3`, `requested_gross_volume_m3`, `pump_budget_m3`, `allocation_shortfall_volume_m3`, `quota_binding`, and `zones`. The count is integer and binding is Boolean. `irrigated_area_m2` sums areas with positive allocated depth. Allocated mean depth and volume are `sum(A_i*x_i)/field_area` and `sum(A_i*x_i)/1000`. Requested volume replaces x with d; shortfall volume is requested minus allocated. `zones` includes Z0 through the final edge with exact keys `zone_id`, `unit_count`, `area_m2`, `area_fraction`, and `mean_depth_mm`; the mean is area-weighted allocated depth or zero for an empty zone.
