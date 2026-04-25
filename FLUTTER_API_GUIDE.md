# TrueLive Portal API Guide for Flutter Developers

Complete guide to integrating the TrueLive Portal API with your Flutter application. This guide provides ready-to-use code examples with both the `http` and `dio` packages.

## Table of Contents

- [Getting Started](#getting-started)
- [Authentication Flow](#authentication-flow)
  - [Login](#login)
  - [Using Access Tokens](#using-access-tokens)
  - [Token Refresh](#token-refresh)
  - [Handling Authentication Errors](#handling-authentication-errors)
  - [Logout](#logout)
- [SureView Endpoints](#sureview-endpoints)
  - [Get Sites](#get-sites)
  - [Get All Sites](#get-all-sites)
  - [Get Cameras](#get-cameras)
  - [Trigger Sync](#trigger-sync)
- [Complete Implementation Examples](#complete-implementation-examples)
- [Error Handling Best Practices](#error-handling-best-practices)

---

## Getting Started

### Base URL

```dart
const String baseUrl = 'https://your-api-domain.com';
const String apiV1 = '/api/v1';
```

### Add Dependencies

Add one of these packages to your `pubspec.yaml`:

**Option 1: Using http package**
```yaml
dependencies:
  http: ^1.1.0
```

**Option 2: Using dio package (recommended for advanced features)**
```yaml
dependencies:
  dio: ^5.4.0
```

### Install Packages

```bash
flutter pub get
```

---

## Authentication Flow

The TrueLive Portal API uses JWT (JSON Web Tokens) for authentication. Each successful login returns an access token and a refresh token.

### Token Details

- **Access Token**: Short-lived token (default: 30 minutes) used for API requests
- **Refresh Token**: Long-lived token (default: 7 days) used to obtain new access tokens
- **Remember Me**: When enabled, access token expires in 7 days instead of 30 minutes

---

## Login

Authenticate a user and receive JWT tokens.

**Endpoint:** `POST /api/v1/auth/login`

**Request Body:**
```json
{
  "username": "string",
  "password": "string",
  "remember_me": false
}
```

**Response (200 OK):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

**Error Response (401 Unauthorized):**
```json
{
  "detail": "Incorrect username or password"
}
```

**Error Response (403 Forbidden):**
```json
{
  "detail": "User account is inactive"
}
```

### Flutter Implementation

#### Using http package

```dart
import 'package:http/http.dart' as http;
import 'dart:convert';

class AuthService {
  static const String baseUrl = 'https://your-api-domain.com';
  static const String apiV1 = '/api/v1';

  Future<Map<String, dynamic>> login({
    required String username,
    required String password,
    bool rememberMe = false,
  }) async {
    final url = Uri.parse('$baseUrl$apiV1/auth/login');

    try {
      final response = await http.post(
        url,
        headers: {
          'Content-Type': 'application/json',
        },
        body: jsonEncode({
          'username': username,
          'password': password,
          'remember_me': rememberMe,
        }),
      );

      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        // Store tokens securely (use flutter_secure_storage)
        return {
          'success': true,
          'data': data,
        };
      } else if (response.statusCode == 401) {
        return {
          'success': false,
          'error': 'Incorrect username or password',
        };
      } else if (response.statusCode == 403) {
        return {
          'success': false,
          'error': 'User account is inactive',
        };
      } else {
        final error = jsonDecode(response.body);
        return {
          'success': false,
          'error': error['detail'] ?? 'Login failed',
        };
      }
    } catch (e) {
      return {
        'success': false,
        'error': 'Network error: ${e.toString()}',
      };
    }
  }
}
```

#### Using dio package

```dart
import 'package:dio/dio.dart';

class AuthService {
  static const String baseUrl = 'https://your-api-domain.com';
  static const String apiV1 = '/api/v1';

  final Dio _dio = Dio(BaseOptions(
    baseUrl: '$baseUrl$apiV1',
    connectTimeout: const Duration(seconds: 10),
    receiveTimeout: const Duration(seconds: 10),
  ));

  Future<Map<String, dynamic>> login({
    required String username,
    required String password,
    bool rememberMe = false,
  }) async {
    try {
      final response = await _dio.post(
        '/auth/login',
        data: {
          'username': username,
          'password': password,
          'remember_me': rememberMe,
        },
      );

      // Store tokens securely (use flutter_secure_storage)
      return {
        'success': true,
        'data': response.data,
      };
    } on DioException catch (e) {
      if (e.response?.statusCode == 401) {
        return {
          'success': false,
          'error': 'Incorrect username or password',
        };
      } else if (e.response?.statusCode == 403) {
        return {
          'success': false,
          'error': 'User account is inactive',
        };
      } else {
        return {
          'success': false,
          'error': e.response?.data['detail'] ?? 'Login failed',
        };
      }
    } catch (e) {
      return {
        'success': false,
        'error': 'Network error: ${e.toString()}',
      };
    }
  }
}
```

#### Example Usage

```dart
final authService = AuthService();

final result = await authService.login(
  username: 'john_doe',
  password: 'SecurePass123',
  rememberMe: true,
);

if (result['success']) {
  final tokens = result['data'];
  print('Access Token: ${tokens['access_token']}');
  print('Expires in: ${tokens['expires_in']} seconds');
  // Store tokens using flutter_secure_storage
} else {
  print('Error: ${result['error']}');
}
```

---

## Using Access Tokens

All protected endpoints require the access token in the `Authorization` header.

### Adding Authorization Header

#### Using http package

```dart
import 'package:http/http.dart' as http;

Future<http.Response> makeAuthenticatedRequest(String accessToken) async {
  final url = Uri.parse('$baseUrl$apiV1/auth/me');

  final response = await http.get(
    url,
    headers: {
      'Authorization': 'Bearer $accessToken',
      'Content-Type': 'application/json',
    },
  );

  return response;
}
```

#### Using dio package with Interceptor (Recommended)

```dart
import 'package:dio/dio.dart';

class ApiClient {
  static const String baseUrl = 'https://your-api-domain.com';
  static const String apiV1 = '/api/v1';

  late Dio _dio;
  String? _accessToken;

  ApiClient() {
    _dio = Dio(BaseOptions(
      baseUrl: '$baseUrl$apiV1',
      connectTimeout: const Duration(seconds: 10),
      receiveTimeout: const Duration(seconds: 10),
    ));

    // Add interceptor to automatically add token to requests
    _dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          if (_accessToken != null) {
            options.headers['Authorization'] = 'Bearer $_accessToken';
          }
          return handler.next(options);
        },
      ),
    );
  }

  void setAccessToken(String token) {
    _accessToken = token;
  }

  void clearAccessToken() {
    _accessToken = null;
  }

  Dio get dio => _dio;
}
```

---

## Token Refresh

When the access token expires, use the refresh token to obtain a new access token without requiring the user to log in again.

**Endpoint:** `POST /api/v1/auth/refresh`

**Request Body:**
```json
{
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

**Response (200 OK):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

**Error Response (401 Unauthorized):**
```json
{
  "detail": "Could not validate credentials"
}
```

### Flutter Implementation

#### Using http package

```dart
Future<Map<String, dynamic>> refreshToken(String refreshToken) async {
  final url = Uri.parse('$baseUrl$apiV1/auth/refresh');

  try {
    final response = await http.post(
      url,
      headers: {
        'Content-Type': 'application/json',
      },
      body: jsonEncode({
        'refresh_token': refreshToken,
      }),
    );

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return {
        'success': true,
        'data': data,
      };
    } else {
      return {
        'success': false,
        'error': 'Token refresh failed',
      };
    }
  } catch (e) {
    return {
      'success': false,
      'error': 'Network error: ${e.toString()}',
    };
  }
}
```

#### Using dio package

```dart
Future<Map<String, dynamic>> refreshToken(String refreshToken) async {
  try {
    final response = await _dio.post(
      '/auth/refresh',
      data: {
        'refresh_token': refreshToken,
      },
    );

    return {
      'success': true,
      'data': response.data,
    };
  } on DioException catch (e) {
    return {
      'success': false,
      'error': e.response?.data['detail'] ?? 'Token refresh failed',
    };
  }
}
```

#### Auto-Refresh Implementation with Dio Interceptor

```dart
class ApiClient {
  late Dio _dio;
  String? _accessToken;
  String? _refreshToken;

  ApiClient() {
    _dio = Dio(BaseOptions(
      baseUrl: '$baseUrl$apiV1',
      connectTimeout: const Duration(seconds: 10),
      receiveTimeout: const Duration(seconds: 10),
    ));

    _dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) {
          if (_accessToken != null) {
            options.headers['Authorization'] = 'Bearer $_accessToken';
          }
          return handler.next(options);
        },
        onError: (error, handler) async {
          // If 401 error and we have a refresh token, try to refresh
          if (error.response?.statusCode == 401 && _refreshToken != null) {
            try {
              // Refresh the token
              final response = await _dio.post(
                '/auth/refresh',
                data: {'refresh_token': _refreshToken},
                options: Options(
                  headers: {'Authorization': null}, // Don't use old token
                ),
              );

              // Update tokens
              _accessToken = response.data['access_token'];
              _refreshToken = response.data['refresh_token'];

              // Store new tokens securely
              // await secureStorage.write(key: 'access_token', value: _accessToken);

              // Retry the original request with new token
              final options = error.requestOptions;
              options.headers['Authorization'] = 'Bearer $_accessToken';

              final retryResponse = await _dio.fetch(options);
              return handler.resolve(retryResponse);
            } catch (e) {
              // Refresh failed, user needs to log in again
              _accessToken = null;
              _refreshToken = null;
              return handler.reject(error);
            }
          }

          return handler.next(error);
        },
      ),
    );
  }

  void setTokens({required String accessToken, required String refreshToken}) {
    _accessToken = accessToken;
    _refreshToken = refreshToken;
  }

  Dio get dio => _dio;
}
```

---

## Handling Authentication Errors

Common authentication errors and how to handle them:

| Status Code | Error | Action |
|-------------|-------|--------|
| 401 | Invalid credentials / Expired token | Prompt user to log in again |
| 403 | User account inactive | Show account status message |
| 422 | Validation error | Show field-specific errors |

### Error Handling Example

```dart
class AuthErrorHandler {
  static String getErrorMessage(int? statusCode, String? detail) {
    switch (statusCode) {
      case 401:
        return detail ?? 'Invalid credentials or session expired';
      case 403:
        return 'Your account is inactive. Please contact support.';
      case 422:
        return 'Please check your input and try again';
      default:
        return detail ?? 'An error occurred. Please try again.';
    }
  }

  static bool shouldRetry(int? statusCode) {
    return statusCode == 401;
  }

  static bool shouldLogout(int? statusCode) {
    return statusCode == 401 || statusCode == 403;
  }
}
```

---

## Logout

**Endpoint:** `POST /api/v1/auth/logout`

**Headers:** Authorization required

**Response (200 OK):**
```json
{
  "message": "Successfully logged out"
}
```

### Flutter Implementation

```dart
Future<void> logout(String accessToken) async {
  try {
    // Call logout endpoint (optional - for audit logging)
    await _dio.post(
      '/auth/logout',
      options: Options(
        headers: {'Authorization': 'Bearer $accessToken'},
      ),
    );
  } catch (e) {
    // Ignore errors during logout
  } finally {
    // Clear tokens locally (required)
    _accessToken = null;
    _refreshToken = null;
    // Clear from secure storage
    // await secureStorage.delete(key: 'access_token');
    // await secureStorage.delete(key: 'refresh_token');
  }
}
```

---

## SureView Endpoints

These endpoints manage sites and cameras from the SureView system.

---

## Get Sites

Retrieve sites filtered by customer ID and optionally by specific site IDs.

**Endpoint:** `POST /api/v1/sureview/get_sites`

**Headers:** Authorization required

**Request Body:**
```json
{
  "customer_id": "CUST123",
  "site_ids": ["SITE001", "SITE002"]
}
```

Note: `site_ids` is optional. If omitted, all sites for the customer are returned.

**Response (200 OK):**
```json
{
  "customer_id": "CUST123",
  "sites": [
    {
      "address": "123 Main St, City, State 12345",
      "telephone": "+1-555-0100",
      "telephone2": "+1-555-0101",
      "telephonePolice": "911",
      "telephoneFire": "911",
      "notes": "24/7 monitoring required",
      "latLong": "40.7128,-74.0060",
      "site_id": "SITE001",
      "name": "Downtown Branch",
      "camera_count": 12
    }
  ]
}
```

**Error Response (401 Unauthorized):**
```json
{
  "detail": "Could not validate credentials"
}
```

### Flutter Implementation

#### Model Class

```dart
class SiteDetail {
  final String? address;
  final String? telephone;
  final String? telephone2;
  final String? telephonePolice;
  final String? telephoneFire;
  final String? notes;
  final String? latLong;
  final String siteId;
  final String name;
  final int cameraCount;

  SiteDetail({
    this.address,
    this.telephone,
    this.telephone2,
    this.telephonePolice,
    this.telephoneFire,
    this.notes,
    this.latLong,
    required this.siteId,
    required this.name,
    required this.cameraCount,
  });

  factory SiteDetail.fromJson(Map<String, dynamic> json) {
    return SiteDetail(
      address: json['address'],
      telephone: json['telephone'],
      telephone2: json['telephone2'],
      telephonePolice: json['telephonePolice'],
      telephoneFire: json['telephoneFire'],
      notes: json['notes'],
      latLong: json['latLong'],
      siteId: json['site_id'],
      name: json['name'],
      cameraCount: json['camera_count'] ?? 0,
    );
  }
}

class GetSitesResponse {
  final String customerId;
  final List<SiteDetail> sites;

  GetSitesResponse({
    required this.customerId,
    required this.sites,
  });

  factory GetSitesResponse.fromJson(Map<String, dynamic> json) {
    return GetSitesResponse(
      customerId: json['customer_id'],
      sites: (json['sites'] as List)
          .map((site) => SiteDetail.fromJson(site))
          .toList(),
    );
  }
}
```

#### Using http package

```dart
Future<GetSitesResponse?> getSites({
  required String accessToken,
  required String customerId,
  List<String>? siteIds,
}) async {
  final url = Uri.parse('$baseUrl$apiV1/sureview/get_sites');

  try {
    final response = await http.post(
      url,
      headers: {
        'Authorization': 'Bearer $accessToken',
        'Content-Type': 'application/json',
      },
      body: jsonEncode({
        'customer_id': customerId,
        if (siteIds != null) 'site_ids': siteIds,
      }),
    );

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return GetSitesResponse.fromJson(data);
    } else {
      print('Error: ${response.statusCode}');
      return null;
    }
  } catch (e) {
    print('Error: $e');
    return null;
  }
}
```

#### Using dio package

```dart
class SureViewService {
  final ApiClient _apiClient;

  SureViewService(this._apiClient);

  Future<GetSitesResponse?> getSites({
    required String customerId,
    List<String>? siteIds,
  }) async {
    try {
      final response = await _apiClient.dio.post(
        '/sureview/get_sites',
        data: {
          'customer_id': customerId,
          if (siteIds != null) 'site_ids': siteIds,
        },
      );

      return GetSitesResponse.fromJson(response.data);
    } on DioException catch (e) {
      print('Error: ${e.response?.statusCode} - ${e.message}');
      return null;
    }
  }
}
```

#### Example Usage

```dart
// Get all sites for a customer
final response = await sureViewService.getSites(
  customerId: 'CUST123',
);

if (response != null) {
  print('Found ${response.sites.length} sites');
  for (var site in response.sites) {
    print('${site.name} - ${site.cameraCount} cameras');
  }
}

// Get specific sites
final response2 = await sureViewService.getSites(
  customerId: 'CUST123',
  siteIds: ['SITE001', 'SITE002'],
);
```

---

## Get All Sites

Retrieve all sites grouped by customer ID. No request body required.

**Endpoint:** `POST /api/v1/sureview/get_all_sites`

**Headers:** Authorization required

**Request Body:** None (empty body)

**Response (200 OK):**
```json
[
  {
    "customer_id": "CUST123",
    "customer_sites": [
      {
        "customer_id": "CUST123",
        "site_id": "SITE001",
        "name": "Downtown Branch",
        "camera_count": 12
      },
      {
        "customer_id": "CUST123",
        "site_id": "SITE002",
        "name": "Uptown Branch",
        "camera_count": 8
      }
    ]
  },
  {
    "customer_id": "CUST456",
    "customer_sites": [
      {
        "customer_id": "CUST456",
        "site_id": "SITE003",
        "name": "Warehouse A",
        "camera_count": 24
      }
    ]
  }
]
```

### Flutter Implementation

#### Model Classes

```dart
class CustomerSiteSummary {
  final String customerId;
  final String siteId;
  final String name;
  final int cameraCount;

  CustomerSiteSummary({
    required this.customerId,
    required this.siteId,
    required this.name,
    required this.cameraCount,
  });

  factory CustomerSiteSummary.fromJson(Map<String, dynamic> json) {
    return CustomerSiteSummary(
      customerId: json['customer_id'],
      siteId: json['site_id'],
      name: json['name'],
      cameraCount: json['camera_count'] ?? 0,
    );
  }
}

class CustomerSitesGroup {
  final String customerId;
  final List<CustomerSiteSummary> customerSites;

  CustomerSitesGroup({
    required this.customerId,
    required this.customerSites,
  });

  factory CustomerSitesGroup.fromJson(Map<String, dynamic> json) {
    return CustomerSitesGroup(
      customerId: json['customer_id'],
      customerSites: (json['customer_sites'] as List)
          .map((site) => CustomerSiteSummary.fromJson(site))
          .toList(),
    );
  }
}
```

#### Using http package

```dart
Future<List<CustomerSitesGroup>?> getAllSites(String accessToken) async {
  final url = Uri.parse('$baseUrl$apiV1/sureview/get_all_sites');

  try {
    final response = await http.post(
      url,
      headers: {
        'Authorization': 'Bearer $accessToken',
        'Content-Type': 'application/json',
      },
    );

    if (response.statusCode == 200) {
      final List<dynamic> data = jsonDecode(response.body);
      return data.map((group) => CustomerSitesGroup.fromJson(group)).toList();
    } else {
      print('Error: ${response.statusCode}');
      return null;
    }
  } catch (e) {
    print('Error: $e');
    return null;
  }
}
```

#### Using dio package

```dart
Future<List<CustomerSitesGroup>?> getAllSites() async {
  try {
    final response = await _apiClient.dio.post('/sureview/get_all_sites');

    final List<dynamic> data = response.data;
    return data.map((group) => CustomerSitesGroup.fromJson(group)).toList();
  } on DioException catch (e) {
    print('Error: ${e.response?.statusCode} - ${e.message}');
    return null;
  }
}
```

#### Example Usage

```dart
final allSites = await sureViewService.getAllSites();

if (allSites != null) {
  for (var customerGroup in allSites) {
    print('Customer: ${customerGroup.customerId}');
    print('Total sites: ${customerGroup.customerSites.length}');

    int totalCameras = customerGroup.customerSites
        .fold(0, (sum, site) => sum + site.cameraCount);
    print('Total cameras: $totalCameras');

    for (var site in customerGroup.customerSites) {
      print('  - ${site.name} (${site.cameraCount} cameras)');
    }
  }
}
```

---

## Get Cameras

Retrieve all cameras for a specific site.

**Endpoint:** `POST /api/v1/sureview/get_cameras`

**Headers:** Authorization required

**Request Body:**
```json
{
  "site_id": "SITE001"
}
```

**Response (200 OK):**
```json
[
  {
    "camera_id": "CAM001",
    "camera_name": "Front Entrance",
    "rtsp_url": "rtsp://10.0.0.100:554/stream1"
  },
  {
    "camera_id": "CAM002",
    "camera_name": "Parking Lot",
    "rtsp_url": "rtsp://10.0.0.101:554/stream1"
  }
]
```

**Error Response (404 Not Found):**
```json
{
  "detail": "Site with id SITE001 not found"
}
```

### Flutter Implementation

#### Model Class

```dart
class CameraDetail {
  final String cameraId;
  final String cameraName;
  final String rtspUrl;

  CameraDetail({
    required this.cameraId,
    required this.cameraName,
    required this.rtspUrl,
  });

  factory CameraDetail.fromJson(Map<String, dynamic> json) {
    return CameraDetail(
      cameraId: json['camera_id'],
      cameraName: json['camera_name'],
      rtspUrl: json['rtsp_url'],
    );
  }
}
```

#### Using http package

```dart
Future<List<CameraDetail>?> getCameras({
  required String accessToken,
  required String siteId,
}) async {
  final url = Uri.parse('$baseUrl$apiV1/sureview/get_cameras');

  try {
    final response = await http.post(
      url,
      headers: {
        'Authorization': 'Bearer $accessToken',
        'Content-Type': 'application/json',
      },
      body: jsonEncode({
        'site_id': siteId,
      }),
    );

    if (response.statusCode == 200) {
      final List<dynamic> data = jsonDecode(response.body);
      return data.map((camera) => CameraDetail.fromJson(camera)).toList();
    } else if (response.statusCode == 404) {
      print('Site not found');
      return null;
    } else {
      print('Error: ${response.statusCode}');
      return null;
    }
  } catch (e) {
    print('Error: $e');
    return null;
  }
}
```

#### Using dio package

```dart
Future<List<CameraDetail>?> getCameras({required String siteId}) async {
  try {
    final response = await _apiClient.dio.post(
      '/sureview/get_cameras',
      data: {'site_id': siteId},
    );

    final List<dynamic> data = response.data;
    return data.map((camera) => CameraDetail.fromJson(camera)).toList();
  } on DioException catch (e) {
    if (e.response?.statusCode == 404) {
      print('Site not found: $siteId');
    } else {
      print('Error: ${e.response?.statusCode} - ${e.message}');
    }
    return null;
  }
}
```

#### Example Usage

```dart
final cameras = await sureViewService.getCameras(siteId: 'SITE001');

if (cameras != null) {
  print('Found ${cameras.length} cameras');
  for (var camera in cameras) {
    print('${camera.cameraName}: ${camera.rtspUrl}');
  }
} else {
  print('No cameras found or site does not exist');
}
```

---

## Trigger Sync

Manually trigger synchronization of SureView devices. This endpoint is restricted to admin and super admin users only.

**Endpoint:** `POST /api/v1/sureview/sync`

**Headers:** Authorization required (Admin/Super Admin only)

**Request Body:** None (empty body)

**Response (200 OK):**
```json
{
  "success": true,
  "message": "SureView sync completed",
  "result": {
    "sites_created": 5,
    "sites_updated": 12,
    "sites_removed": 1,
    "cameras_created": 48,
    "cameras_updated": 103,
    "cameras_removed": 7,
    "errors": 0
  }
}
```

**Error Response (403 Forbidden):**
```json
{
  "detail": "Insufficient permissions"
}
```

**Error Response (500 Internal Server Error):**
```json
{
  "detail": "Sync failed: Connection timeout"
}
```

### Flutter Implementation

#### Model Class

```dart
class SyncResult {
  final bool success;
  final String message;
  final Map<String, dynamic> result;

  SyncResult({
    required this.success,
    required this.message,
    required this.result,
  });

  factory SyncResult.fromJson(Map<String, dynamic> json) {
    return SyncResult(
      success: json['success'] ?? false,
      message: json['message'] ?? '',
      result: json['result'] ?? {},
    );
  }

  int get sitesCreated => result['sites_created'] ?? 0;
  int get sitesUpdated => result['sites_updated'] ?? 0;
  int get sitesRemoved => result['sites_removed'] ?? 0;
  int get camerasCreated => result['cameras_created'] ?? 0;
  int get camerasUpdated => result['cameras_updated'] ?? 0;
  int get camerasRemoved => result['cameras_removed'] ?? 0;
  int get errors => result['errors'] ?? 0;
}
```

#### Using http package

```dart
Future<SyncResult?> triggerSync(String accessToken) async {
  final url = Uri.parse('$baseUrl$apiV1/sureview/sync');

  try {
    final response = await http.post(
      url,
      headers: {
        'Authorization': 'Bearer $accessToken',
        'Content-Type': 'application/json',
      },
    );

    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      return SyncResult.fromJson(data);
    } else if (response.statusCode == 403) {
      print('Insufficient permissions - admin access required');
      return null;
    } else if (response.statusCode == 500) {
      final error = jsonDecode(response.body);
      print('Sync failed: ${error['detail']}');
      return null;
    } else {
      print('Error: ${response.statusCode}');
      return null;
    }
  } catch (e) {
    print('Error: $e');
    return null;
  }
}
```

#### Using dio package

```dart
Future<SyncResult?> triggerSync() async {
  try {
    final response = await _apiClient.dio.post('/sureview/sync');
    return SyncResult.fromJson(response.data);
  } on DioException catch (e) {
    if (e.response?.statusCode == 403) {
      print('Insufficient permissions - admin access required');
    } else if (e.response?.statusCode == 500) {
      print('Sync failed: ${e.response?.data['detail']}');
    } else {
      print('Error: ${e.response?.statusCode} - ${e.message}');
    }
    return null;
  }
}
```

#### Example Usage

```dart
// Trigger sync (admin only)
final result = await sureViewService.triggerSync();

if (result != null && result.success) {
  print('Sync completed successfully!');
  print('Sites: ${result.sitesCreated} created, ${result.sitesUpdated} updated');
  print('Cameras: ${result.camerasCreated} created, ${result.camerasUpdated} updated');

  if (result.errors > 0) {
    print('Warning: ${result.errors} errors occurred during sync');
  }
} else {
  print('Sync failed or insufficient permissions');
}
```

---

## Complete Implementation Examples

### Complete API Client with Dio

```dart
import 'package:dio/dio.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class TrueLiveApiClient {
  static const String baseUrl = 'https://your-api-domain.com';
  static const String apiV1 = '/api/v1';

  late Dio _dio;
  final FlutterSecureStorage _secureStorage = const FlutterSecureStorage();
  String? _accessToken;
  String? _refreshToken;

  TrueLiveApiClient() {
    _dio = Dio(BaseOptions(
      baseUrl: '$baseUrl$apiV1',
      connectTimeout: const Duration(seconds: 10),
      receiveTimeout: const Duration(seconds: 10),
    ));

    _setupInterceptors();
  }

  void _setupInterceptors() {
    _dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) async {
          // Add access token if available
          if (_accessToken != null) {
            options.headers['Authorization'] = 'Bearer $_accessToken';
          }
          return handler.next(options);
        },
        onError: (error, handler) async {
          // Handle 401 errors with automatic token refresh
          if (error.response?.statusCode == 401 && _refreshToken != null) {
            try {
              await _refreshAccessToken();

              // Retry original request with new token
              final options = error.requestOptions;
              options.headers['Authorization'] = 'Bearer $_accessToken';

              final response = await _dio.fetch(options);
              return handler.resolve(response);
            } catch (e) {
              // Refresh failed, clear tokens and redirect to login
              await logout();
              return handler.reject(error);
            }
          }

          return handler.next(error);
        },
      ),
    );
  }

  Future<void> _refreshAccessToken() async {
    final response = await _dio.post(
      '/auth/refresh',
      data: {'refresh_token': _refreshToken},
      options: Options(headers: {'Authorization': null}),
    );

    _accessToken = response.data['access_token'];
    _refreshToken = response.data['refresh_token'];

    // Save tokens
    await _secureStorage.write(key: 'access_token', value: _accessToken);
    await _secureStorage.write(key: 'refresh_token', value: _refreshToken);
  }

  Future<void> loadTokens() async {
    _accessToken = await _secureStorage.read(key: 'access_token');
    _refreshToken = await _secureStorage.read(key: 'refresh_token');
  }

  Future<Map<String, dynamic>> login({
    required String username,
    required String password,
    bool rememberMe = false,
  }) async {
    try {
      final response = await _dio.post(
        '/auth/login',
        data: {
          'username': username,
          'password': password,
          'remember_me': rememberMe,
        },
      );

      _accessToken = response.data['access_token'];
      _refreshToken = response.data['refresh_token'];

      // Save tokens securely
      await _secureStorage.write(key: 'access_token', value: _accessToken);
      await _secureStorage.write(key: 'refresh_token', value: _refreshToken);

      return {'success': true, 'data': response.data};
    } on DioException catch (e) {
      return {
        'success': false,
        'error': e.response?.data['detail'] ?? 'Login failed',
      };
    }
  }

  Future<void> logout() async {
    try {
      await _dio.post('/auth/logout');
    } catch (e) {
      // Ignore errors
    } finally {
      _accessToken = null;
      _refreshToken = null;
      await _secureStorage.delete(key: 'access_token');
      await _secureStorage.delete(key: 'refresh_token');
    }
  }

  // SureView methods
  Future<GetSitesResponse?> getSites({
    required String customerId,
    List<String>? siteIds,
  }) async {
    try {
      final response = await _dio.post(
        '/sureview/get_sites',
        data: {
          'customer_id': customerId,
          if (siteIds != null) 'site_ids': siteIds,
        },
      );
      return GetSitesResponse.fromJson(response.data);
    } catch (e) {
      return null;
    }
  }

  Future<List<CustomerSitesGroup>?> getAllSites() async {
    try {
      final response = await _dio.post('/sureview/get_all_sites');
      final List<dynamic> data = response.data;
      return data.map((g) => CustomerSitesGroup.fromJson(g)).toList();
    } catch (e) {
      return null;
    }
  }

  Future<List<CameraDetail>?> getCameras({required String siteId}) async {
    try {
      final response = await _dio.post(
        '/sureview/get_cameras',
        data: {'site_id': siteId},
      );
      final List<dynamic> data = response.data;
      return data.map((c) => CameraDetail.fromJson(c)).toList();
    } catch (e) {
      return null;
    }
  }

  Future<SyncResult?> triggerSync() async {
    try {
      final response = await _dio.post('/sureview/sync');
      return SyncResult.fromJson(response.data);
    } catch (e) {
      return null;
    }
  }

  bool get isAuthenticated => _accessToken != null;

  Dio get dio => _dio;
}
```

### Usage in Flutter App

```dart
void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final apiClient = TrueLiveApiClient();
  await apiClient.loadTokens();

  runApp(MyApp(apiClient: apiClient));
}

class MyApp extends StatelessWidget {
  final TrueLiveApiClient apiClient;

  const MyApp({Key? key, required this.apiClient}) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: apiClient.isAuthenticated ? HomeScreen() : LoginScreen(),
    );
  }
}

class LoginScreen extends StatefulWidget {
  @override
  _LoginScreenState createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _usernameController = TextEditingController();
  final _passwordController = TextEditingController();
  bool _rememberMe = false;
  bool _isLoading = false;

  Future<void> _login() async {
    setState(() => _isLoading = true);

    final result = await apiClient.login(
      username: _usernameController.text,
      password: _passwordController.text,
      rememberMe: _rememberMe,
    );

    setState(() => _isLoading = false);

    if (result['success']) {
      Navigator.of(context).pushReplacement(
        MaterialPageRoute(builder: (_) => HomeScreen()),
      );
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(result['error'])),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('Login')),
      body: Padding(
        padding: EdgeInsets.all(16),
        child: Column(
          children: [
            TextField(
              controller: _usernameController,
              decoration: InputDecoration(labelText: 'Username'),
            ),
            TextField(
              controller: _passwordController,
              decoration: InputDecoration(labelText: 'Password'),
              obscureText: true,
            ),
            CheckboxListTile(
              title: Text('Remember me'),
              value: _rememberMe,
              onChanged: (value) => setState(() => _rememberMe = value!),
            ),
            SizedBox(height: 20),
            ElevatedButton(
              onPressed: _isLoading ? null : _login,
              child: _isLoading
                  ? CircularProgressIndicator()
                  : Text('Login'),
            ),
          ],
        ),
      ),
    );
  }
}

class HomeScreen extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Text('TrueLive Portal'),
        actions: [
          IconButton(
            icon: Icon(Icons.logout),
            onPressed: () async {
              await apiClient.logout();
              Navigator.of(context).pushReplacement(
                MaterialPageRoute(builder: (_) => LoginScreen()),
              );
            },
          ),
        ],
      ),
      body: SitesListScreen(),
    );
  }
}

class SitesListScreen extends StatefulWidget {
  @override
  _SitesListScreenState createState() => _SitesListScreenState();
}

class _SitesListScreenState extends State<SitesListScreen> {
  List<CustomerSitesGroup>? _allSites;
  bool _isLoading = true;

  @override
  void initState() {
    super.initState();
    _loadSites();
  }

  Future<void> _loadSites() async {
    setState(() => _isLoading = true);

    final sites = await apiClient.getAllSites();

    setState(() {
      _allSites = sites;
      _isLoading = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_isLoading) {
      return Center(child: CircularProgressIndicator());
    }

    if (_allSites == null || _allSites!.isEmpty) {
      return Center(child: Text('No sites found'));
    }

    return ListView.builder(
      itemCount: _allSites!.length,
      itemBuilder: (context, index) {
        final customerGroup = _allSites![index];
        return ExpansionTile(
          title: Text('Customer: ${customerGroup.customerId}'),
          subtitle: Text('${customerGroup.customerSites.length} sites'),
          children: customerGroup.customerSites.map((site) {
            return ListTile(
              title: Text(site.name),
              subtitle: Text('${site.cameraCount} cameras'),
              onTap: () {
                Navigator.push(
                  context,
                  MaterialPageRoute(
                    builder: (_) => CamerasScreen(siteId: site.siteId),
                  ),
                );
              },
            );
          }).toList(),
        );
      },
    );
  }
}

class CamerasScreen extends StatefulWidget {
  final String siteId;

  const CamerasScreen({Key? key, required this.siteId}) : super(key: key);

  @override
  _CamerasScreenState createState() => _CamerasScreenState();
}

class _CamerasScreenState extends State<CamerasScreen> {
  List<CameraDetail>? _cameras;
  bool _isLoading = true;

  @override
  void initState() {
    super.initState();
    _loadCameras();
  }

  Future<void> _loadCameras() async {
    setState(() => _isLoading = true);

    final cameras = await apiClient.getCameras(siteId: widget.siteId);

    setState(() {
      _cameras = cameras;
      _isLoading = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('Cameras')),
      body: _isLoading
          ? Center(child: CircularProgressIndicator())
          : _cameras == null || _cameras!.isEmpty
              ? Center(child: Text('No cameras found'))
              : ListView.builder(
                  itemCount: _cameras!.length,
                  itemBuilder: (context, index) {
                    final camera = _cameras![index];
                    return ListTile(
                      title: Text(camera.cameraName),
                      subtitle: Text(camera.rtspUrl),
                      leading: Icon(Icons.videocam),
                    );
                  },
                ),
    );
  }
}
```

---

## Error Handling Best Practices

### 1. Network Error Handling

```dart
try {
  final response = await apiClient.getSites(customerId: 'CUST123');
  // Handle success
} on DioException catch (e) {
  if (e.type == DioExceptionType.connectionTimeout) {
    print('Connection timeout');
  } else if (e.type == DioExceptionType.receiveTimeout) {
    print('Receive timeout');
  } else if (e.type == DioExceptionType.badResponse) {
    print('Server error: ${e.response?.statusCode}');
  } else if (e.type == DioExceptionType.cancel) {
    print('Request cancelled');
  } else {
    print('Network error: ${e.message}');
  }
} catch (e) {
  print('Unexpected error: $e');
}
```

### 2. Validation Error Handling

```dart
if (e.response?.statusCode == 422) {
  final errors = e.response?.data['detail'];
  if (errors is List) {
    for (var error in errors) {
      print('${error['loc']}: ${error['msg']}');
    }
  }
}
```

### 3. Token Expiry Handling

The interceptor in the complete implementation handles this automatically, but you can also handle it manually:

```dart
if (e.response?.statusCode == 401) {
  // Try to refresh token
  final refreshed = await apiClient.refreshToken();
  if (refreshed) {
    // Retry the request
    return await apiClient.getSites(customerId: 'CUST123');
  } else {
    // Redirect to login
    Navigator.pushReplacementNamed(context, '/login');
  }
}
```

### 4. User-Friendly Error Messages

```dart
String getUserFriendlyError(DioException e) {
  switch (e.response?.statusCode) {
    case 400:
      return 'Invalid request. Please check your input.';
    case 401:
      return 'Session expired. Please log in again.';
    case 403:
      return 'You don\'t have permission to perform this action.';
    case 404:
      return 'The requested resource was not found.';
    case 500:
      return 'Server error. Please try again later.';
    default:
      if (e.type == DioExceptionType.connectionTimeout) {
        return 'Connection timeout. Please check your internet.';
      }
      return 'An error occurred. Please try again.';
  }
}
```

---

## Additional Resources

### Secure Token Storage

Always use `flutter_secure_storage` to store tokens:

```yaml
dependencies:
  flutter_secure_storage: ^9.0.0
```

```dart
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

final storage = FlutterSecureStorage();

// Write
await storage.write(key: 'access_token', value: accessToken);

// Read
final token = await storage.read(key: 'access_token');

// Delete
await storage.delete(key: 'access_token');
```

### Environment Configuration

Use environment variables for API URLs:

```dart
class AppConfig {
  static const String baseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'https://api.example.com',
  );
}
```

Run with:
```bash
flutter run --dart-define=API_BASE_URL=https://your-api-domain.com
```

---

## Summary

This guide covered:

1. **Authentication Flow** - Login, token management, refresh, and logout
2. **SureView Endpoints** - Getting sites, cameras, and triggering syncs
3. **Complete Examples** - Production-ready code with error handling
4. **Best Practices** - Secure storage, error handling, and user experience

For production use:
- Always store tokens securely using `flutter_secure_storage`
- Implement automatic token refresh with interceptors
- Handle all error cases gracefully
- Use environment variables for configuration
- Add proper loading states and user feedback

For support or questions, refer to the API documentation or contact the Ahmad.
