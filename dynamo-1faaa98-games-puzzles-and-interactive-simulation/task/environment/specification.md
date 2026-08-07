The files under `/app/input` define six independent field-season simulations with different grids, boundary topology, crop development, observation calendars, water quality, pump budgets, and contingency policies. `manifest.json` has the exact schema `{"schema_version": 1, "campaigns": [{"campaign_id": string, "directory": absolute-path}, ...]}` and bytewise campaign order. Each campaign directory contains `job_ticket.json`, `field_boundary.geojson`, `soil_map_units.geojson`, `soil_horizons.csv`, `vegetation_index.csv`, `weather.csv`, `initial_depletion.csv`, `initial_salinity.csv`, and `irrigation_events.geojson`. Dates are ISO 8601 civil dates with inclusive simulation endpoints. Coordinates use the metre-based projected CRS in the ticket.

The ticket has exact keys `campaign_id`, `crs`, `grid`, `simulation`, `crop`, `irrigation`, `salinity`, `pump`, and `response_frontier`. `grid` contains `origin_x`, `origin_y`, `cell_width_m`, `cell_height_m`, `row_direction`, and `column_direction`; directions are `north_to_south` and `west_to_east`. `crop` contains `root_depth_curve`, `depletion_fraction`, `kc_slope`, `kc_intercept`, `kc_min`, and `kc_max`. `irrigation` contains `efficiency` and `max_application_mm`. `salinity` contains `rainfall_ec_ds_m`, `crop_threshold_ec_ds_m`, `yield_slope_per_ds_m`, `minimum_stress_coefficient`, `leaching_efficiency`, `new_root_zone_ec_ds_m`, `minimum_solution_depth_mm`, and `leaching_requirement_mm_per_ds_m`. `pump` contains `volume_budget_m3`, `water_deficit_priority_weight`, `salinity_priority_weight`, and `stress_history_priority_weight`. `response_frontier` contains `satisfaction_ratio` and `scenarios`. The scenarios are ordered `critical`, `severe`, `restricted`, `nominal`; each has exactly `scenario_id`, `nominal_budget_fraction`, `water_weight_multiplier`, `salinity_weight_multiplier`, and `history_weight_multiplier`. Fractions are strictly increasing and finish at one.

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

At the terminal date, calculate three dimensionless need components for every unit: `water_need_index = Dr/max(TAW,minimum_solution_depth_mm)`, `salt_need_index = max(final_EC/crop_threshold_ec_ds_m-1,0)`, and `history_need_index = (stress_days+salinity_stress_days)/Ndays`. The requested gross depth `request_mm` is the lesser of `irrigation.max_application_mm` and the sum of: zero when final Dr is at most final RAW, otherwise `Dr/irrigation.efficiency`; and `salt_need_index*crop_threshold_ec_ds_m*leaching_requirement_mm_per_ds_m/irrigation.efficiency`.

Build a five-point response frontier. The first four points are the ticket scenarios. Scenario s has budget `B_s = nominal_budget_fraction * pump.volume_budget_m3` and priority

`w_is = 1 + water_deficit_priority_weight*water_need_index_i*water_weight_multiplier_s + salinity_priority_weight*salt_need_index_i*salinity_weight_multiplier_s + stress_history_priority_weight*history_need_index_i*history_weight_multiplier_s`.

Append a fifth `recovery` point whose budget is total requested volume and whose three multipliers are one. At each point independently choose `0 <= x_is <= request_i` to minimize `sum(A_i*w_is*(request_i-x_is)^2)` subject to `sum(A_i*x_is/1000) <= B_s`. When requested volume is no larger than the scenario budget, use the full requests and depth shadow price zero. Otherwise use `x_is=max(0,request_i-lambda_s/(2*w_is))`. Find `lambda_s` with exactly 100 bisections over `[0,max_i(2*w_is*request_i)]`, moving the lower endpoint when trial volume exceeds the budget and the upper endpoint otherwise, then use the final upper endpoint.

For every scenario report priority, allocated depth, and shortfall. `activation_scenario` is the first scenario in frontier order with allocated depth greater than `1e-12`; it is `none` if request is zero. `satisfaction_scenario` is the first scenario whose allocated depth is at least `satisfaction_ratio*request_mm`, or `none` for a zero request. `robustness_score` is one for a zero request and otherwise the arithmetic mean of `allocated_depth/request_mm` across the four scarcity scenarios. `frontier_gain_mm` is recovery depth minus critical depth.

For each scenario the certificate reports `scenario_id`, `budget_m3`, `allocated_volume_m3`, `shortfall_volume_m3`, Boolean `binding`, `depth_shadow_price`, `weighted_shortfall_cost`, integer `active_unit_count`, integer `satisfied_unit_count`, `mean_service_ratio`, and `transition_volume_m3`. Cost is `sum(A_i*w_is*(request_i-x_is)^2)`. Service ratios are one for zero-request units and `x_is/request_i` otherwise. A unit is counted as satisfied exactly when `x_is >= satisfaction_ratio*request_i`, so zero-request units are satisfied. Transition volume is the current allocated volume minus the previous point's allocation, with zero as the predecessor of `critical`.

`/app/output/allocation-frontier.geojson` is a FeatureCollection with exact top-level keys `type` and `features`. Each feature has exact keys `type`, `geometry`, and `properties`, type `Feature`, and the clipped unit geometry. Properties have exactly: string `campaign_id`, string `unit_id`, integer `row`, integer `column`, finite-number `clipped_area_m2`, `area_fraction`, `taw_mm`, `raw_mm`, `final_dr_mm`, `final_kc`, `final_ec_ds_m`, `minimum_k_sal`, `request_mm`, `water_need_index`, `salt_need_index`, `history_need_index`, `seasonal_etc_mm`, `seasonal_effective_irrigation_mm`, `seasonal_drainage_mm`, `seasonal_leached_salt_index`, integer `stress_days`, integer `salinity_stress_days`, finite-number `minimum_ks`; for each of `critical`, `severe`, `restricted`, `nominal`, and `recovery`, finite-number `<scenario>_priority`, `<scenario>_depth_mm`, and `<scenario>_shortfall_mm`; string `activation_scenario`, string `satisfaction_scenario`, finite-number `robustness_score`, and finite-number `frontier_gain_mm`. Order features by bytewise campaign ID, integer row, then integer column.

`/app/output/allocation-frontier.csv` contains identical properties in that exact order. Integers use base-ten integer text; all other numbers use finite, non-exponent decimal text.

`/app/output/optimality-certificate.json` has exact top-level keys `schema_version` and `campaigns`, schema version 2, and bytewise campaign order. Each campaign has exact keys `campaign_id`, `analysis_unit_count`, `field_area_m2`, `requested_volume_m3`, `satisfaction_ratio`, and `scenarios`; scenario records use the certificate keys defined above and frontier order. All numeric calculation and output uses finite float64 values without intermediate rounding.
