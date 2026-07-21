# TrueLive Portal API Guide for Laravel Developers

Complete guide to integrating the TrueLive Portal API with your Laravel application. This guide provides ready-to-use code examples with Guzzle HTTP client and Laravel best practices.

## Table of Contents

- [Getting Started](#getting-started)
- [Authentication Flow](#authentication-flow)
  - [Login](#login)
  - [Using Access Tokens](#using-access-tokens)
  - [Token Refresh](#token-refresh)
  - [Handling Authentication Errors](#handling-authentication-errors)
  - [Logout](#logout)
- [Complete Implementation Examples](#complete-implementation-examples)
- [Error Handling Best Practices](#error-handling-best-practices)

---

## Getting Started

### Base URL Configuration

Add the API base URL to your `.env` file:

```env
TRUELIVE_API_BASE_URL=https://your-api-domain.com
TRUELIVE_API_VERSION=v1
```

Update `config/services.php`:

```php
<?php

return [
    // ... other services

    'truelive' => [
        'base_url' => env('TRUELIVE_API_BASE_URL', 'https://api.example.com'),
        'api_version' => env('TRUELIVE_API_VERSION', 'v1'),
    ],
];
```

### Install Guzzle HTTP Client

Guzzle is included with Laravel by default. If you need to install it:

```bash
composer require guzzlehttp/guzzle
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

### Laravel Implementation

#### Create Auth Service

Create a new service class `app/Services/TrueLiveAuthService.php`:

```php
<?php

namespace App\Services;

use GuzzleHttp\Client;
use GuzzleHttp\Exception\GuzzleException;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Log;

class TrueLiveAuthService
{
    protected Client $client;
    protected string $baseUrl;

    public function __construct()
    {
        $this->baseUrl = config('services.truelive.base_url');
        $apiVersion = config('services.truelive.api_version');

        $this->client = new Client([
            'base_uri' => "{$this->baseUrl}/api/{$apiVersion}/",
            'timeout' => 10.0,
            'headers' => [
                'Content-Type' => 'application/json',
                'Accept' => 'application/json',
            ],
        ]);
    }

    /**
     * Login and get JWT tokens
     *
     * @param string $username
     * @param string $password
     * @param bool $rememberMe
     * @return array
     */
    public function login(string $username, string $password, bool $rememberMe = false): array
    {
        try {
            $response = $this->client->post('auth/login', [
                'json' => [
                    'username' => $username,
                    'password' => $password,
                    'remember_me' => $rememberMe,
                ],
            ]);

            $data = json_decode($response->getBody()->getContents(), true);

            // Store tokens in session or cache
            session([
                'truelive_access_token' => $data['access_token'],
                'truelive_refresh_token' => $data['refresh_token'],
                'truelive_token_expires_at' => now()->addSeconds($data['expires_in']),
            ]);

            return [
                'success' => true,
                'data' => $data,
            ];

        } catch (GuzzleException $e) {
            $statusCode = $e->getResponse()?->getStatusCode();
            $errorBody = $e->getResponse()?->getBody()->getContents();
            $errorData = json_decode($errorBody, true);

            Log::error('TrueLive login failed', [
                'status' => $statusCode,
                'error' => $errorData,
            ]);

            return [
                'success' => false,
                'error' => $errorData['detail'] ?? 'Login failed',
                'status_code' => $statusCode,
            ];
        }
    }

    /**
     * Get current access token from session
     *
     * @return string|null
     */
    public function getAccessToken(): ?string
    {
        return session('truelive_access_token');
    }

    /**
     * Get current refresh token from session
     *
     * @return string|null
     */
    public function getRefreshToken(): ?string
    {
        return session('truelive_refresh_token');
    }

    /**
     * Check if user is authenticated
     *
     * @return bool
     */
    public function isAuthenticated(): bool
    {
        return session()->has('truelive_access_token');
    }
}
```

#### Create Login Controller

```php
<?php

namespace App\Http\Controllers\Auth;

use App\Http\Controllers\Controller;
use App\Services\TrueLiveAuthService;
use Illuminate\Http\Request;
use Illuminate\Http\JsonResponse;

class TrueLiveLoginController extends Controller
{
    protected TrueLiveAuthService $authService;

    public function __construct(TrueLiveAuthService $authService)
    {
        $this->authService = $authService;
    }

    /**
     * Handle login request
     */
    public function login(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'username' => 'required|string',
            'password' => 'required|string',
            'remember_me' => 'sometimes|boolean',
        ]);

        $result = $this->authService->login(
            $validated['username'],
            $validated['password'],
            $validated['remember_me'] ?? false
        );

        if ($result['success']) {
            return response()->json([
                'message' => 'Login successful',
                'data' => $result['data'],
            ]);
        }

        return response()->json([
            'message' => $result['error'],
        ], $result['status_code'] ?? 500);
    }
}
```

#### Example Usage in Blade Form

```html
<form id="loginForm">
    @csrf
    <div class="mb-3">
        <label for="username" class="form-label">Username</label>
        <input type="text" class="form-control" id="username" name="username" required>
    </div>
    <div class="mb-3">
        <label for="password" class="form-label">Password</label>
        <input type="password" class="form-control" id="password" name="password" required>
    </div>
    <div class="mb-3 form-check">
        <input type="checkbox" class="form-check-input" id="remember_me" name="remember_me">
        <label class="form-check-label" for="remember_me">Remember me</label>
    </div>
    <button type="submit" class="btn btn-primary">Login</button>
</form>

<script>
document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();

    const formData = new FormData(e.target);
    const data = {
        username: formData.get('username'),
        password: formData.get('password'),
        remember_me: formData.get('remember_me') === 'on',
    };

    try {
        const response = await fetch('/api/truelive/login', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-TOKEN': document.querySelector('meta[name="csrf-token"]').content,
            },
            body: JSON.stringify(data),
        });

        const result = await response.json();

        if (response.ok) {
            window.location.href = '/dashboard';
        } else {
            alert(result.message);
        }
    } catch (error) {
        console.error('Login error:', error);
        alert('An error occurred during login');
    }
});
</script>
```

---

## Using Access Tokens

All protected endpoints require the access token in the `Authorization` header.

### Adding Authorization Header

#### Basic Example with Guzzle

```php
<?php

use GuzzleHttp\Client;

$accessToken = session('truelive_access_token');

$client = new Client([
    'base_uri' => config('services.truelive.base_url'),
    'headers' => [
        'Authorization' => "Bearer {$accessToken}",
        'Content-Type' => 'application/json',
        'Accept' => 'application/json',
    ],
]);

$response = $client->get('/api/v1/auth/me');
$userData = json_decode($response->getBody()->getContents(), true);
```

#### Using Middleware for Automatic Token Injection

Create middleware `app/Http/Middleware/TrueLiveApiAuth.php`:

```php
<?php

namespace App\Http\Middleware;

use Closure;
use Illuminate\Http\Request;
use App\Services\TrueLiveAuthService;

class TrueLiveApiAuth
{
    protected TrueLiveAuthService $authService;

    public function __construct(TrueLiveAuthService $authService)
    {
        $this->authService = $authService;
    }

    public function handle(Request $request, Closure $next)
    {
        if (!$this->authService->isAuthenticated()) {
            return redirect()->route('truelive.login')
                ->with('error', 'Please log in to continue');
        }

        // Add token to request for use in controllers
        $request->merge([
            'truelive_access_token' => $this->authService->getAccessToken(),
        ]);

        return $next($request);
    }
}
```

Register middleware in `app/Http/Kernel.php`:

```php
protected $routeMiddleware = [
    // ... other middleware
    'truelive.auth' => \App\Http\Middleware\TrueLiveApiAuth::class,
];
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

### Laravel Implementation

Add to `TrueLiveAuthService`:

```php
/**
 * Refresh access token using refresh token
 *
 * @return array
 */
public function refreshToken(): array
{
    $refreshToken = $this->getRefreshToken();

    if (!$refreshToken) {
        return [
            'success' => false,
            'error' => 'No refresh token available',
        ];
    }

    try {
        $response = $this->client->post('auth/refresh', [
            'json' => [
                'refresh_token' => $refreshToken,
            ],
        ]);

        $data = json_decode($response->getBody()->getContents(), true);

        // Update tokens in session
        session([
            'truelive_access_token' => $data['access_token'],
            'truelive_refresh_token' => $data['refresh_token'],
            'truelive_token_expires_at' => now()->addSeconds($data['expires_in']),
        ]);

        return [
            'success' => true,
            'data' => $data,
        ];

    } catch (GuzzleException $e) {
        $errorBody = $e->getResponse()?->getBody()->getContents();
        $errorData = json_decode($errorBody, true);

        Log::error('Token refresh failed', ['error' => $errorData]);

        // Clear invalid tokens
        session()->forget(['truelive_access_token', 'truelive_refresh_token', 'truelive_token_expires_at']);

        return [
            'success' => false,
            'error' => $errorData['detail'] ?? 'Token refresh failed',
        ];
    }
}

/**
 * Check if token is expired and refresh if needed
 *
 * @return bool
 */
public function ensureValidToken(): bool
{
    $expiresAt = session('truelive_token_expires_at');

    if (!$expiresAt || now()->greaterThan($expiresAt)) {
        $result = $this->refreshToken();
        return $result['success'];
    }

    return true;
}
```

#### Auto-Refresh Middleware

Update `TrueLiveApiAuth` middleware:

```php
public function handle(Request $request, Closure $next)
{
    if (!$this->authService->isAuthenticated()) {
        return redirect()->route('truelive.login')
            ->with('error', 'Please log in to continue');
    }

    // Automatically refresh token if expired
    if (!$this->authService->ensureValidToken()) {
        return redirect()->route('truelive.login')
            ->with('error', 'Session expired. Please log in again.');
    }

    $request->merge([
        'truelive_access_token' => $this->authService->getAccessToken(),
    ]);

    return $next($request);
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

```php
<?php

namespace App\Exceptions;

use GuzzleHttp\Exception\ClientException;
use Illuminate\Http\JsonResponse;

class TrueLiveApiErrorHandler
{
    /**
     * Handle TrueLive API errors
     *
     * @param \Exception $e
     * @return JsonResponse
     */
    public static function handle(\Exception $e): JsonResponse
    {
        if ($e instanceof ClientException) {
            $statusCode = $e->getResponse()->getStatusCode();
            $errorBody = $e->getResponse()->getBody()->getContents();
            $errorData = json_decode($errorBody, true);

            $message = match($statusCode) {
                401 => 'Invalid credentials or session expired',
                403 => 'Your account is inactive. Please contact support.',
                404 => 'Resource not found',
                422 => 'Validation error: ' . ($errorData['detail'] ?? 'Invalid input'),
                default => $errorData['detail'] ?? 'An error occurred',
            };

            return response()->json([
                'error' => $message,
                'details' => $errorData,
            ], $statusCode);
        }

        return response()->json([
            'error' => 'An unexpected error occurred',
            'message' => $e->getMessage(),
        ], 500);
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

### Laravel Implementation

Add to `TrueLiveAuthService`:

```php
/**
 * Logout user and clear tokens
 *
 * @return array
 */
public function logout(): array
{
    $accessToken = $this->getAccessToken();

    if ($accessToken) {
        try {
            // Call logout endpoint (for audit logging)
            $this->client->post('auth/logout', [
                'headers' => [
                    'Authorization' => "Bearer {$accessToken}",
                ],
            ]);
        } catch (GuzzleException $e) {
            // Ignore errors during logout
            Log::warning('Logout API call failed', ['error' => $e->getMessage()]);
        }
    }

    // Always clear local tokens
    session()->forget(['truelive_access_token', 'truelive_refresh_token', 'truelive_token_expires_at']);

    return [
        'success' => true,
        'message' => 'Logged out successfully',
    ];
}
```

Add to `TrueLiveLoginController`:

```php
/**
 * Handle logout request
 */
public function logout(): JsonResponse
{
    $result = $this->authService->logout();

    return response()->json([
        'message' => $result['message'],
    ]);
}
```

---

## Complete Implementation Examples

### Complete API Service Class

```php
<?php

namespace App\Services;

use GuzzleHttp\Client;
use GuzzleHttp\Exception\GuzzleException;
use Illuminate\Support\Facades\Log;

class TrueLiveApiService
{
    protected Client $client;
    protected string $accessToken;
    protected string $refreshToken;

    public function __construct()
    {
        $baseUrl = config('services.truelive.base_url');
        $apiVersion = config('services.truelive.api_version');

        $this->client = new Client([
            'base_uri' => "{$baseUrl}/api/{$apiVersion}/",
            'timeout' => 30.0,
        ]);

        $this->loadTokensFromSession();
    }

    protected function loadTokensFromSession(): void
    {
        $this->accessToken = session('truelive_access_token', '');
        $this->refreshToken = session('truelive_refresh_token', '');
    }

    protected function saveTokensToSession(array $tokenData): void
    {
        session([
            'truelive_access_token' => $tokenData['access_token'],
            'truelive_refresh_token' => $tokenData['refresh_token'],
            'truelive_token_expires_at' => now()->addSeconds($tokenData['expires_in']),
        ]);

        $this->accessToken = $tokenData['access_token'];
        $this->refreshToken = $tokenData['refresh_token'];
    }

    protected function getAuthHeaders(): array
    {
        return [
            'Authorization' => "Bearer {$this->accessToken}",
            'Content-Type' => 'application/json',
            'Accept' => 'application/json',
        ];
    }

    /**
     * Login
     */
    public function login(string $username, string $password, bool $rememberMe = false): array
    {
        try {
            $response = $this->client->post('auth/login', [
                'json' => [
                    'username' => $username,
                    'password' => $password,
                    'remember_me' => $rememberMe,
                ],
            ]);

            $data = json_decode($response->getBody()->getContents(), true);
            $this->saveTokensToSession($data);

            return ['success' => true, 'data' => $data];
        } catch (GuzzleException $e) {
            return $this->handleException($e);
        }
    }

    /**
     * Refresh token
     */
    public function refreshToken(): array
    {
        try {
            $response = $this->client->post('auth/refresh', [
                'json' => ['refresh_token' => $this->refreshToken],
            ]);

            $data = json_decode($response->getBody()->getContents(), true);
            $this->saveTokensToSession($data);

            return ['success' => true, 'data' => $data];
        } catch (GuzzleException $e) {
            session()->forget(['truelive_access_token', 'truelive_refresh_token', 'truelive_token_expires_at']);
            return $this->handleException($e);
        }
    }

    /**
     * Logout
     */
    public function logout(): array
    {
        try {
            $this->client->post('auth/logout', [
                'headers' => $this->getAuthHeaders(),
            ]);
        } catch (GuzzleException $e) {
            Log::warning('Logout API call failed', ['error' => $e->getMessage()]);
        }

        session()->forget(['truelive_access_token', 'truelive_refresh_token', 'truelive_token_expires_at']);
        return ['success' => true, 'message' => 'Logged out successfully'];
    }

    /**
     * Handle Guzzle exceptions
     */
    protected function handleException(GuzzleException $e): array
    {
        $statusCode = $e->getResponse()?->getStatusCode();
        $errorBody = $e->getResponse()?->getBody()->getContents();
        $errorData = json_decode($errorBody, true);

        Log::error('TrueLive API error', [
            'status_code' => $statusCode,
            'error' => $errorData,
        ]);

        return [
            'success' => false,
            'error' => $errorData['detail'] ?? 'API request failed',
            'status_code' => $statusCode,
        ];
    }

    /**
     * Check if authenticated
     */
    public function isAuthenticated(): bool
    {
        return !empty($this->accessToken);
    }
}
```

### Complete Controller Example

```php
<?php

namespace App\Http\Controllers;

use App\Services\TrueLiveApiService;
use Illuminate\Http\Request;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\Validator;

class TrueLiveApiController extends Controller
{
    protected TrueLiveApiService $apiService;

    public function __construct(TrueLiveApiService $apiService)
    {
        $this->apiService = $apiService;
    }

    /**
     * Login
     */
    public function login(Request $request): JsonResponse
    {
        $validator = Validator::make($request->all(), [
            'username' => 'required|string',
            'password' => 'required|string',
            'remember_me' => 'sometimes|boolean',
        ]);

        if ($validator->fails()) {
            return response()->json([
                'error' => 'Validation failed',
                'details' => $validator->errors(),
            ], 422);
        }

        $result = $this->apiService->login(
            $request->input('username'),
            $request->input('password'),
            $request->input('remember_me', false)
        );

        return $this->apiResponse($result);
    }

    /**
     * Logout
     */
    public function logout(): JsonResponse
    {
        $result = $this->apiService->logout();
        return response()->json($result);
    }

    /**
     * Format API response
     */
    protected function apiResponse(array $result): JsonResponse
    {
        if ($result['success']) {
            return response()->json($result['data']);
        }

        return response()->json([
            'error' => $result['error'],
        ], $result['status_code'] ?? 500);
    }
}
```

### Route Configuration

Add to `routes/api.php`:

```php
<?php

use App\Http\Controllers\TrueLiveApiController;
use Illuminate\Support\Facades\Route;

Route::prefix('truelive')->group(function () {
    // Public routes
    Route::post('/login', [TrueLiveApiController::class, 'login']);

    // Protected routes
    Route::middleware('truelive.auth')->group(function () {
        Route::post('/logout', [TrueLiveApiController::class, 'logout']);
    });
});
```

---

## Error Handling Best Practices

### 1. Custom Exception Handler

Create `app/Exceptions/TrueLiveApiException.php`:

```php
<?php

namespace App\Exceptions;

use Exception;

class TrueLiveApiException extends Exception
{
    protected int $statusCode;
    protected array $errorData;

    public function __construct(string $message, int $statusCode = 500, array $errorData = [])
    {
        parent::__construct($message);
        $this->statusCode = $statusCode;
        $this->errorData = $errorData;
    }

    public function render()
    {
        return response()->json([
            'error' => $this->getMessage(),
            'details' => $this->errorData,
        ], $this->statusCode);
    }

    public function getStatusCode(): int
    {
        return $this->statusCode;
    }

    public function getErrorData(): array
    {
        return $this->errorData;
    }
}
```

### 2. Validation Error Handling

```php
use Illuminate\Validation\ValidationException;

try {
    $validated = $request->validate([
        'customer_id' => 'required|string',
        'site_ids' => 'sometimes|array',
    ]);
} catch (ValidationException $e) {
    return response()->json([
        'error' => 'Validation failed',
        'details' => $e->errors(),
    ], 422);
}
```

### 3. Network Error Handling

```php
use GuzzleHttp\Exception\ConnectException;
use GuzzleHttp\Exception\RequestException;

try {
    $result = $this->apiService->getSites($customerId);
} catch (ConnectException $e) {
    return response()->json([
        'error' => 'Cannot connect to TrueLive API',
        'message' => 'Please check your internet connection',
    ], 503);
} catch (RequestException $e) {
    $statusCode = $e->getResponse()?->getStatusCode() ?? 500;

    return response()->json([
        'error' => 'API request failed',
        'message' => $e->getMessage(),
    ], $statusCode);
}
```

### 4. User-Friendly Error Messages

Create a helper class:

```php
<?php

namespace App\Helpers;

class TrueLiveErrorHelper
{
    public static function getUserFriendlyMessage(int $statusCode, string $detail = ''): string
    {
        return match($statusCode) {
            400 => 'Invalid request. Please check your input.',
            401 => 'Session expired. Please log in again.',
            403 => 'You don\'t have permission to perform this action.',
            404 => 'The requested resource was not found.',
            422 => 'Validation error: ' . $detail,
            500 => 'Server error. Please try again later.',
            503 => 'Service unavailable. Please try again later.',
            default => 'An error occurred. Please try again.',
        };
    }
}
```

Usage:

```php
if (!$result['success']) {
    $message = TrueLiveErrorHelper::getUserFriendlyMessage(
        $result['status_code'] ?? 500,
        $result['error']
    );

    return response()->json(['error' => $message], $result['status_code'] ?? 500);
}
```

---

## Additional Resources

### Environment Variables

Add to `.env`:

```env
TRUELIVE_API_BASE_URL=https://your-api-domain.com
TRUELIVE_API_VERSION=v1
TRUELIVE_API_TIMEOUT=30
TRUELIVE_TOKEN_CACHE_MINUTES=1800
```

---

## Summary

This guide covered:

1. **Authentication Flow** - Login, token management, refresh, and logout with Laravel
2. **Complete Examples** - Production-ready Laravel code with services and controllers
3. **Best Practices** - Error handling and validation

### Key Takeaways for Laravel Developers:

- Use **Guzzle HTTP client** for API requests
- Store tokens in **session** or **cache** (use encrypted session for production)
- Implement **middleware** for automatic token injection and refresh
- Use **service classes** to encapsulate API logic
- Implement proper **error handling** with try-catch blocks
- Use **Laravel's validation** for request validation
- Consider **caching** responses to reduce API calls
- Use **queue jobs** for long-running operations like sync

### Production Checklist:

- ✅ Store tokens securely (encrypted session/database)
- ✅ Implement automatic token refresh in middleware
- ✅ Handle all HTTP error codes gracefully
- ✅ Log API errors for debugging
- ✅ Use environment variables for configuration
- ✅ Implement rate limiting for API calls
- ✅ Add request/response logging for audit trail
- ✅ Use HTTPS for all API communications

For support or questions, refer to the API documentation at `/api/v1/docs` or contact Ahmad.
