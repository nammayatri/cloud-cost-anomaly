pipeline {
  parameters {
    booleanParam(name: 'AWS_PROD', defaultValue: true, description: 'Push image to AWS prod ECR (147728078333, ap-south-1) — where the live cloud-cost-report CronJob runs')
    booleanParam(name: 'GCP_PROD', defaultValue: true, description: 'Push image to GCP prod Artifact Registry (ny-prod, asia-south1)')
  }

  agent {
    kubernetes {
      label 'dind-agent'
    }
  }

  environment {
    IMAGE_NAME = 'cost-anomaly-cron'

    // AWS Production
    AWS_ACCOUNT_ID_PROD = '147728078333'
    AWS_ECR_REGION = 'ap-south-1'
    AWS_ECR_REPO = "${AWS_ACCOUNT_ID_PROD}.dkr.ecr.${AWS_ECR_REGION}.amazonaws.com/${IMAGE_NAME}"

    // GCP Production
    GCP_PROJECT_PROD = 'ny-prod'
    GCP_AR_PROD = "asia-south1-docker.pkg.dev/${GCP_PROJECT_PROD}"
  }

  stages {
    stage('Initialize') {
      steps {
        script {
          env.LAST_COMMIT_HASH = sh(script: "git rev-parse HEAD", returnStdout: true).trim().substring(0, 6)
        }
      }
    }

    stage('Build image') {
      steps {
        sh "docker build --no-cache -t ${env.IMAGE_NAME}:${env.LAST_COMMIT_HASH} ."
      }
    }

    stage('Push to AWS Prod (1477...)') {
      when {
        expression { params.AWS_PROD }
      }
      steps {
        script {
          echo "Pushing to AWS Production Account: ${env.AWS_ACCOUNT_ID_PROD}"

          // Login
          sh "aws ecr get-login-password --region ${env.AWS_ECR_REGION} | docker login --username AWS --password-stdin ${env.AWS_ECR_REPO}"

          // Tag and Push
          sh "docker tag ${env.IMAGE_NAME}:${env.LAST_COMMIT_HASH} ${env.AWS_ECR_REPO}:${env.LAST_COMMIT_HASH}"
          sh "docker push ${env.AWS_ECR_REPO}:${env.LAST_COMMIT_HASH}"

          // Latest tag
          sh "docker tag ${env.IMAGE_NAME}:${env.LAST_COMMIT_HASH} ${env.AWS_ECR_REPO}:latest"
          sh "docker push ${env.AWS_ECR_REPO}:latest"
        }
      }
    }

    stage('Push to GCP Prod (ny-prod)') {
      when {
        expression { params.GCP_PROD }
      }
      steps {
        withCredentials([file(credentialsId: 'gcp-sa-key-prod', variable: 'GCP_KEY_FILE_PROD')]) {
          script {
            echo "Pushing to GCP Production Project: ${env.GCP_PROJECT_PROD}"

            // Login
            sh 'cat $GCP_KEY_FILE_PROD | docker login -u _json_key --password-stdin https://asia-south1-docker.pkg.dev'

            // Tag and Push
            sh "docker tag ${env.IMAGE_NAME}:${env.LAST_COMMIT_HASH} ${env.GCP_AR_PROD}/${env.IMAGE_NAME}/${env.IMAGE_NAME}:${env.LAST_COMMIT_HASH}"
            sh "docker push ${env.GCP_AR_PROD}/${env.IMAGE_NAME}/${env.IMAGE_NAME}:${env.LAST_COMMIT_HASH}"

            // Latest tag
            sh "docker tag ${env.IMAGE_NAME}:${env.LAST_COMMIT_HASH} ${env.GCP_AR_PROD}/${env.IMAGE_NAME}/${env.IMAGE_NAME}:latest"
            sh "docker push ${env.GCP_AR_PROD}/${env.IMAGE_NAME}/${env.IMAGE_NAME}:latest"
          }
        }
      }
    }
  }
}
